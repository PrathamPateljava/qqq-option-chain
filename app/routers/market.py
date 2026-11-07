"""Market data for the dashboard: /api/expirations and /api/chain.
Same behaviour as the old payoff_server.py: one upstream call at a time, short caches so
several tabs share a fetch, and the last snapshot is reused while the feed rate-limits."""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

log = logging.getLogger("payoff.market")
router = APIRouter(prefix="/api", tags=["market"])

SYMBOL_RE = re.compile(r"^\$?[A-Z][A-Z./]{0,9}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHAIN_TTL = 2.0          # seconds; several browser tabs share one upstream call
EXPIRATIONS_TTL = 300.0
BACKOFF = 15.0           # when rate-limited, keep serving the last snapshot this long

_source = None
_lock = threading.Lock()  # one upstream call at a time
_cache: dict = {}


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class AlpacaSource:
    name = "alpaca"

    def __init__(self, strikes: int = 60):
        from app.feeds import alpaca_feed
        self.client = alpaca_feed.AlpacaClient()
        self.strikes = strikes

    def check(self):
        self.client.check()

    def expirations(self, symbol):
        return self.client.expirations(symbol)

    def chain(self, symbol, expiration):
        return self.client.chain(symbol, expiration, self.strikes)


class TastySource:
    name = "tastytrade"

    def __init__(self, strikes: int = 60):
        from app.feeds import tasty_feed
        self.client = tasty_feed.TastyClient()
        self.strikes = strikes

    def check(self):
        self.client.access_token()

    def expirations(self, symbol):
        return self.client.expirations(symbol)

    def chain(self, symbol, expiration):
        return self.client.chain(symbol, expiration, self.strikes)


class SchwabSource:
    name = "schwab"

    def __init__(self, strikes: int = 60):
        from app.feeds import qqq_chain
        self.qc = qqq_chain

    def check(self):
        self.qc.get_access_token()  # any browser login happens here, before serving

    def expirations(self, symbol, days=180):
        chain = self.qc.fetch_chain(symbol, days=days, strikes=2)
        exps = sorted({key.split(":")[0] for side in ("callExpDateMap", "putExpDateMap")
                       for key in (chain.get(side) or {})})
        return {"symbol": symbol, "spot": _num(chain.get("underlyingPrice")), "source": self.name,
                "isDelayed": bool(chain.get("isDelayed")), "expirations": exps}

    def chain(self, symbol, expiration):
        chain = self.qc.fetch_chain(symbol, strikes=None, from_date=expiration, to_date=expiration)
        contracts = []
        for row in self.qc.chain_to_df(chain).to_dict("records"):
            dte = _num(row.get("dte"))
            contracts.append({
                "symbol": row["symbol"], "putCall": row["putCall"],
                "expiration": str(row["expiration"]), "dte": int(dte) if dte is not None else None,
                "strike": _num(row["strikePrice"]),
                "bid": _num(row["bid"]), "ask": _num(row["ask"]), "last": _num(row["last"]),
                "mark": _num(row["mark"]), "volume": _num(row["totalVolume"]),
                "oi": _num(row["openInterest"]), "iv": _num(row["iv"]),
                "delta": _num(row["delta"]), "gamma": _num(row["gamma"]),
                "theta": _num(row["theta"]), "vega": _num(row["vega"]),
            })
        return {"symbol": symbol, "expiration": expiration, "source": self.name,
                "spot": _num(chain.get("underlyingPrice")), "isDelayed": bool(chain.get("isDelayed")),
                "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "contracts": contracts}


SOURCES = {"alpaca": AlpacaSource, "tastytrade": TastySource, "schwab": SchwabSource}


def set_source(source) -> None:
    global _source
    _source = source
    _cache.clear()


def make_source(name: str, strikes: int):
    if name not in SOURCES:
        raise ValueError(f"DATA_SOURCE must be one of {', '.join(SOURCES)}, not {name!r}")
    return SOURCES[name](strikes)


def _cached(key, ttl, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            value = fn()
        except Exception as err:
            if hit and getattr(err, "retry_later", False):  # rate-limited: serve the last snapshot
                _cache[key] = (time.time() + BACKOFF - ttl, hit[1])
                return hit[1]
            raise
        _cache[key] = (time.time(), value)
        return value


def _error(msg: str, status: int):
    return JSONResponse({"error": msg, "source": getattr(_source, "name", None)}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _serve(key, ttl, fn):
    if _source is None:
        return _error("No market data source is connected.", 503)
    try:
        return JSONResponse(_cached(key, ttl, fn), headers={"Cache-Control": "no-store"})
    except Exception as err:  # upstream or network trouble: tell the page what happened
        log.warning("%s: %s: %s", _source.name, type(err).__name__, err)
        return _error(str(err), 502)


# Sync handlers: FastAPI runs them in a thread pool, so the blocking feed calls are fine.
@router.get("/expirations")
def expirations(symbol: str = Query("QQQ", max_length=12)):
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return _error(f"Not a valid symbol: {symbol!r}", 400)
    return _serve(("exp", symbol), EXPIRATIONS_TTL, lambda: _source.expirations(symbol))


@router.get("/chain")
def chain(symbol: str = Query("QQQ", max_length=12), expiration: str = Query("", max_length=10)):
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.match(symbol):
        return _error(f"Not a valid symbol: {symbol!r}", 400)
    if not DATE_RE.match(expiration):
        return _error("expiration must look like YYYY-MM-DD", 400)
    return _serve(("chain", symbol, expiration), CHAIN_TTL, lambda: _source.chain(symbol, expiration))
