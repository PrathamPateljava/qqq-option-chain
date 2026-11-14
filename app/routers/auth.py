"""Sign up, log in, log out, who am I, and password reset by email (forgot -> link -> reset).
An admin can still reset a password by hand with `python -m app.cli reset-password <email>`."""
from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import mailer
from app.config import get_settings
from app.db import get_db, get_sessionmaker
from app.models import User
from app.security import (RateLimiter, client_ip, current_user, end_all_sessions, end_session, hash_password,
                          issue_email_token, maybe_rehash, redeem_email_token, start_session, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger("payoff")

MIN_PASSWORD = 10
RESET_MINUTES = 60
login_by_email = RateLimiter(limit=8, window_s=15 * 60)   # failed logins per account
login_by_ip = RateLimiter(limit=30, window_s=15 * 60)     # failed logins per address
signup_by_ip = RateLimiter(limit=10, window_s=60 * 60)    # accounts created per address
forgot_by_email = RateLimiter(limit=3, window_s=60 * 60)  # reset emails per address, so nobody gets flooded
forgot_by_ip = RateLimiter(limit=10, window_s=60 * 60)    # reset requests per network


class Credentials(BaseModel):
    email: EmailStr = Field(max_length=254)
    password: str = Field(min_length=1, max_length=256)


class ForgotIn(BaseModel):
    email: EmailStr = Field(max_length=254)


class ResetIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    email: str


def _out(user: User) -> UserOut:
    return UserOut(email=user.email)


@router.post("/signup", response_model=UserOut, status_code=201)
def signup(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    ip = client_ip(request)
    if signup_by_ip.blocked(ip):
        raise HTTPException(429, "Too many sign-ups from this network. Try again later.")
    if len(body.password) < MIN_PASSWORD:
        raise HTTPException(422, f"Use a password of at least {MIN_PASSWORD} characters.")
    user = User(email=body.email.lower(), password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An account with that email already exists. Log in instead.")
    signup_by_ip.hit(ip)
    start_session(db, user, response)
    return _out(user)


@router.post("/login", response_model=UserOut)
def login(body: Credentials, request: Request, response: Response, db: Session = Depends(get_db)):
    email, ip = body.email.lower(), client_ip(request)
    if login_by_email.blocked(email) or login_by_ip.blocked(ip):
        raise HTTPException(429, "Too many failed attempts. Wait 15 minutes and try again.")
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if not verify_password(user, body.password):
        login_by_email.hit(email)
        login_by_ip.hit(ip)
        raise HTTPException(401, "Email or password is incorrect.")
    login_by_email.reset(email)
    maybe_rehash(db, user, body.password)
    start_session(db, user, response)
    return _out(user)


@router.post("/logout", status_code=204, response_class=Response)
def logout(request: Request, db: Session = Depends(get_db)):
    response = Response(status_code=204)
    end_session(db, request, response)
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return _out(user)


@router.post("/forgot", status_code=202)
def forgot(body: ForgotIn, request: Request, background: BackgroundTasks):
    """Emails a reset link if the address has an account. The reply is identical either
    way, and the lookup happens after it's sent, so this can't reveal who has an account."""
    email, ip = body.email.lower(), client_ip(request)
    if forgot_by_ip.blocked(ip) or forgot_by_email.blocked(email):
        raise HTTPException(429, "Too many reset requests. Check your inbox and spam folder, or try again in an hour.")
    forgot_by_ip.hit(ip)
    forgot_by_email.hit(email)
    # Links point at APP_URL, or else at the address this request came in on: its host has
    # already passed the allowlist, and the scheme comes from the proxy (https on Render).
    site = get_settings().app_url.strip().rstrip("/") or str(request.base_url).rstrip("/")
    background.add_task(_send_reset_link, email, site)
    return {"ok": True}


def _send_reset_link(email: str, site: str) -> None:
    try:
        with get_sessionmaker()() as db:
            user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
            if user is None:
                return
            token = issue_email_token(db, user, "reset", timedelta(minutes=RESET_MINUTES))
    except Exception:  # noqa: BLE001 - the reply has gone; log it for the admin
        log.exception("Couldn't create a password-reset link")
        return
    link = f"{site}/#reset={token}"
    mailer.send_logged(mailer.password_reset(email, link, RESET_MINUTES))


@router.post("/reset", response_model=UserOut)
def reset(body: ResetIn, response: Response, db: Session = Depends(get_db)):
    """Sets a new password from an emailed link, logs out every other browser, and logs this one in."""
    if len(body.password) < MIN_PASSWORD:  # checked first, so a short password doesn't use up the link
        raise HTTPException(422, f"Use a password of at least {MIN_PASSWORD} characters.")
    user = redeem_email_token(db, body.token, "reset")
    if user is None:
        raise HTTPException(400, "This reset link has expired or has already been used. Ask for a new one.")
    user.password_hash = hash_password(body.password)
    end_all_sessions(db, user)
    db.commit()
    login_by_email.reset(user.email)
    start_session(db, user, response)
    return _out(user)
