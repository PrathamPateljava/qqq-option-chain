"""Admin commands, run on a machine with DATABASE_URL set:

    python -m app.cli list-users
    python -m app.cli reset-password someone@example.com     # prompts, or --generate
    python -m app.cli delete-user someone@example.com
    python -m app.cli send-test-email you@example.com        # checks the email settings

People can reset their own password with "Forgot password?" on the login screen. The
manual reset is a fallback: it also logs the user out everywhere. Send the new password
over a channel you trust, and ask them to choose their own with "Forgot password?".
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import func, select

from app import mailer
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Strategy, User
from app.routers.auth import MIN_PASSWORD
from app.security import end_all_sessions, hash_password


def _find(db, email: str) -> User:
    user = db.execute(select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
    if user is None:
        sys.exit(f"No account for {email}")
    return user


def list_users(_args) -> None:
    with get_sessionmaker()() as db:
        rows = db.execute(
            select(User.email, User.created_at, func.count(Strategy.id))
            .outerjoin(Strategy, Strategy.user_id == User.id).group_by(User.id).order_by(User.created_at)
        ).all()
        for email, created, n in rows:
            print(f"{email:<40} joined {created:%Y-%m-%d}  {n} strategies")
        print(f"{len(rows)} account(s)")


def reset_password(args) -> None:
    with get_sessionmaker()() as db:
        user = _find(db, args.email)
        if args.generate:
            password = secrets.token_urlsafe(12)
        else:
            password = getpass.getpass("New password: ")
            if password != getpass.getpass("Repeat it: "):
                sys.exit("Passwords don't match.")
        if len(password) < MIN_PASSWORD:
            sys.exit(f"Use at least {MIN_PASSWORD} characters.")
        user.password_hash = hash_password(password)
        end_all_sessions(db, user)
        db.commit()
        print(f"Password reset for {user.email}; all their sessions were logged out.")
        if args.generate:
            print(f"Temporary password: {password}")


def delete_user(args) -> None:
    with get_sessionmaker()() as db:
        user = _find(db, args.email)
        if not args.yes and input(f"Delete {user.email} and all their strategies? Type 'delete': ") != "delete":
            sys.exit("Cancelled.")
        db.delete(user)
        db.commit()
        print(f"Deleted {args.email}.")


def send_test_email(args) -> None:
    settings = get_settings()
    backend = settings.email_backend.strip().lower()
    if backend == "console":
        sys.exit("Nothing sent: EMAIL_BACKEND is console (the default), which only prints emails in the terminal.\n"
                 "To send real email, add EMAIL_BACKEND=brevo, EMAIL_FROM and BREVO_API_KEY to .env.")
    for problem in mailer.config_problems(settings):
        print(f"Warning: {problem}")
    try:
        mailer.send(mailer.setup_check(args.email))
    except mailer.EmailError as err:
        sys.exit(f"Not sent: {err}")
    print(f"Sent a test email to {args.email} through {backend}. If it isn't in the inbox, check spam.")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="Payoff admin commands")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-users").set_defaults(fn=list_users)
    p = sub.add_parser("reset-password")
    p.add_argument("email")
    p.add_argument("--generate", action="store_true", help="generate and print a temporary password")
    p.set_defaults(fn=reset_password)
    p = sub.add_parser("delete-user")
    p.add_argument("email")
    p.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(fn=delete_user)
    p = sub.add_parser("send-test-email")
    p.add_argument("email")
    p.set_defaults(fn=send_test_email)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
