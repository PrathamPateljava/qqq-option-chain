"""Saved strategies. Every query is scoped to the logged-in user; another user's
strategy answers 404, exactly like one that doesn't exist."""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Strategy, User
from app.security import current_user

router = APIRouter(prefix="/api/strategies", tags=["strategies"])

MAX_PER_USER = 500
SYMBOL_RE = re.compile(r"^\$?[A-Z][A-Z./]{0,9}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class Leg(BaseModel):
    """One option leg, in the same shape the dashboard keeps in memory."""
    model_config = ConfigDict(extra="ignore")

    id: str = Field(max_length=40)
    include: bool = True
    side: Literal[1, -1]
    qty: int = Field(ge=1, le=100_000)
    type: Literal["C", "P"]
    strike: float = Field(gt=0, lt=1_000_000)
    exp: str
    entry: float = Field(ge=0, lt=1_000_000)
    symbol: str | None = Field(default=None, max_length=40)
    lastIv: float | None = Field(default=None, ge=0, le=20)
    openedAt: str | None = Field(default=None, max_length=40)
    exitPrice: float | None = Field(default=None, ge=0, lt=1_000_000)
    closedAt: str | None = Field(default=None, max_length=40)

    @field_validator("exp")
    @classmethod
    def _exp(cls, v: str) -> str:
        if not DATE_RE.match(v):
            raise ValueError("expiration must look like YYYY-MM-DD")
        return v


class Scenario(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ivShift: float = Field(default=0, ge=-100, le=100)    # vol points
    targetMin: int = Field(default=0, ge=0, le=10_000_000)
    spotPct: float = Field(default=0, ge=-99, le=1000)
    r: float = Field(default=0.04, ge=0, le=0.2)
    q: float = Field(default=0.006, ge=0, le=0.2)


def _symbol(v: str) -> str:
    v = v.strip().upper()
    if not SYMBOL_RE.match(v):
        raise ValueError("not a valid symbol")
    return v


class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    symbol: str
    legs: list[Leg] = Field(max_length=50)
    scenario: Scenario = Field(default_factory=Scenario)
    notes: str = Field(default="", max_length=10_000)

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return _symbol(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name can't be blank")
        return v


class StrategyPatch(BaseModel):
    """Partial update: rename, edit notes, or save new legs/scenario."""
    name: str | None = Field(default=None, min_length=1, max_length=100)
    symbol: str | None = None
    legs: list[Leg] | None = Field(default=None, max_length=50)
    scenario: Scenario | None = None
    notes: str | None = Field(default=None, max_length=10_000)

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v):
        return None if v is None else _symbol(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        if v is None:
            return v
        v = v.strip()
        if not v:
            raise ValueError("name can't be blank")
        return v


class StrategySummary(BaseModel):
    id: uuid.UUID
    name: str
    symbol: str
    leg_count: int
    open_legs: int
    created_at: datetime
    updated_at: datetime


class StrategyOut(BaseModel):
    id: uuid.UUID
    name: str
    symbol: str
    legs: list[dict]
    scenario: dict
    notes: str
    created_at: datetime
    updated_at: datetime


def _summary(s: Strategy) -> StrategySummary:
    legs = s.legs or []
    return StrategySummary(id=s.id, name=s.name, symbol=s.symbol, leg_count=len(legs),
                           open_legs=sum(1 for l in legs if l.get("exitPrice") is None),
                           created_at=s.created_at, updated_at=s.updated_at)


def _out(s: Strategy) -> StrategyOut:
    return StrategyOut(id=s.id, name=s.name, symbol=s.symbol, legs=s.legs or [], scenario=s.scenario or {},
                       notes=s.notes or "", created_at=s.created_at, updated_at=s.updated_at)


def _owned(db: Session, user: User, strategy_id: uuid.UUID) -> Strategy:
    s = db.execute(select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user.id)
                   ).scalar_one_or_none()
    if s is None:
        raise HTTPException(404, "Strategy not found.")
    return s


@router.get("", response_model=list[StrategySummary])
def list_strategies(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(Strategy).where(Strategy.user_id == user.id)
                      .order_by(Strategy.updated_at.desc())).scalars()
    return [_summary(s) for s in rows]


@router.post("", response_model=StrategyOut, status_code=201)
def create_strategy(body: StrategyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    count = db.execute(select(func.count()).select_from(Strategy).where(Strategy.user_id == user.id)).scalar_one()
    if count >= MAX_PER_USER:
        raise HTTPException(409, f"You can save up to {MAX_PER_USER} strategies. Delete some first.")
    s = Strategy(user_id=user.id, name=body.name, symbol=body.symbol,
                 legs=[l.model_dump() for l in body.legs], scenario=body.scenario.model_dump(), notes=body.notes)
    db.add(s)
    db.commit()
    db.refresh(s)
    return _out(s)


@router.get("/{strategy_id}", response_model=StrategyOut)
def get_strategy(strategy_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _out(_owned(db, user, strategy_id))


@router.patch("/{strategy_id}", response_model=StrategyOut)
def update_strategy(strategy_id: uuid.UUID, body: StrategyPatch, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    s = _owned(db, user, strategy_id)
    if body.name is not None:
        s.name = body.name
    if body.symbol is not None:
        s.symbol = body.symbol
    if body.legs is not None:
        s.legs = [l.model_dump() for l in body.legs]
    if body.scenario is not None:
        s.scenario = body.scenario.model_dump()
    if body.notes is not None:
        s.notes = body.notes
    s.updated_at = func.now()
    db.commit()
    db.refresh(s)
    return _out(s)


@router.delete("/{strategy_id}", status_code=204, response_class=Response)
def delete_strategy(strategy_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_owned(db, user, strategy_id))
    db.commit()
    return Response(status_code=204)
