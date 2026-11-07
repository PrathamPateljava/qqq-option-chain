"""Database tables: users, login sessions and saved strategies."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # stored lower-cased
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    sessions: Mapped[list[LoginSession]] = relationship(back_populates="user", cascade="all, delete-orphan",
                                                        passive_deletes=True)
    strategies: Mapped[list[Strategy]] = relationship(back_populates="user", cascade="all, delete-orphan",
                                                      passive_deletes=True)


class LoginSession(Base):
    """A logged-in browser. `id` is the SHA-256 of the cookie token, so a database leak
    does not hand out usable cookies."""
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User] = relationship(back_populates="sessions")


class Strategy(Base):
    __tablename__ = "strategies"
    __table_args__ = (Index("ix_strategies_user_updated", "user_id", "updated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(12))
    legs: Mapped[list] = mapped_column(JSONType, default=list)
    scenario: Mapped[dict] = mapped_column(JSONType, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

    user: Mapped[User] = relationship(back_populates="strategies")
