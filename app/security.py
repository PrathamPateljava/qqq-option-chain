"""Password hashing, login sessions, CSRF protection and a small rate limiter."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings
from app.db import get_db
from app.models import LoginSession, User

COOKIE = "payoff_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "payoff"

_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


# ---- passwords ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(user: User | None, password: str) -> bool:
    """Constant-ish time: hashes even when the user doesn't exist, so response times
    don't reveal which emails have accounts."""
    try:
        return _hasher.verify(user.password_hash if user else _DUMMY_HASH, password) and user is not None
    except (VerificationError, InvalidHashError):
        return False


def maybe_rehash(db: Session, user: User, password: str) -> None:
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
        db.commit()


# ---- sessions ----------------------------------------------------------------------------
def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Session, user: User, response: Response) -> None:
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.session_days)
    db.add(LoginSession(id=_digest(token), user_id=user.id, expires_at=expires))
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id,
                                          LoginSession.expires_at < datetime.now(timezone.utc)))
    db.commit()
    response.set_cookie(COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
                        secure=settings.cookie_secure, samesite="lax", path="/")


def end_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        db.execute(delete(LoginSession).where(LoginSession.id == _digest(token)))
        db.commit()
    response.delete_cookie(COOKIE, path="/", httponly=True, secure=get_settings().cookie_secure, samesite="lax")


def end_all_sessions(db: Session, user: User) -> None:
    db.execute(delete(LoginSession).where(LoginSession.user_id == user.id))


def optional_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token or len(token) > 200:
        return None
    row = db.execute(
        select(LoginSession).where(LoginSession.id == _digest(token),
                                   LoginSession.expires_at > datetime.now(timezone.utc))
    ).scalar_one_or_none()
    return row.user if row else None


def current_user(user: User | None = Depends(optional_user)) -> User:
    if user is None:
        raise HTTPException(status_code=401, detail="Log in to continue.")
    return user


# ---- CSRF --------------------------------------------------------------------------------
class CSRFMiddleware(BaseHTTPMiddleware):
    """State-changing API calls must carry a custom header (which a cross-site form or
    image can't send, and cross-site fetch can't send without a CORS preflight this app
    never approves), and, when the browser sends an Origin, it must be this site."""

    async def dispatch(self, request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/"):
            if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return JSONResponse({"error": "Missing request header."}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != "null":
                host = (request.headers.get("host") or "").lower()
                if urlparse(origin).netloc.lower() != host:
                    return JSONResponse({"error": "Cross-site request blocked."}, status_code=403)
        return await call_next(request)


# ---- rate limiting -----------------------------------------------------------------------
class RateLimiter:
    """In-memory sliding window. Fine for one server process; use Redis if you scale out."""

    def __init__(self, limit: int, window_s: float):
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._trim(key, time.monotonic())) >= self.limit

    def hit(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._trim(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
