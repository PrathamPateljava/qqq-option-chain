"""Sign up, log in, log out, and who am I. Password resets are done by an admin with
`python -m app.cli reset-password <email>` (no email service yet)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import (RateLimiter, client_ip, current_user, end_session, hash_password, maybe_rehash,
                          start_session, verify_password)

router = APIRouter(prefix="/api/auth", tags=["auth"])

MIN_PASSWORD = 10
login_by_email = RateLimiter(limit=8, window_s=15 * 60)   # failed logins per account
login_by_ip = RateLimiter(limit=30, window_s=15 * 60)     # failed logins per address
signup_by_ip = RateLimiter(limit=10, window_s=60 * 60)    # accounts created per address


class Credentials(BaseModel):
    email: EmailStr = Field(max_length=254)
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
