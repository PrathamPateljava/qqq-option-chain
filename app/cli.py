"""Admin commands, run on a machine with DATABASE_URL set:

    python -m app.cli list-users
    python -m app.cli reset-password someone@example.com     # prompts, or --generate
    python -m app.cli delete-user someone@example.com

A password reset also logs the user out everywhere. Send the new password to the user
over a channel you trust, and ask them to change it (once a change-password screen exists).
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys

from sqlalchemy import func, select

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
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
