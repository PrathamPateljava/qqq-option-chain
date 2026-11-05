#!/usr/bin/env python3
"""
tasty_feed.py - option chain snapshots from the tastytrade API (OAuth + REST quotes).

One-time setup
  1. Open and fund a tastytrade account (its REST market data is for funded accounts).
  2. On my.tastytrade.com go to Manage > My Profile > API > OAuth Applications.
     Create an application (the read scope is enough) and copy the client secret,
     then click "Create Grant" and copy the refresh token. Neither is shown again.
  3. Put TASTY_CLIENT_SECRET and TASTY_REFRESH_TOKEN in .env (see .env.example)
     or export them as environment variables.

Check your setup:   python tasty_feed.py QQQ
The refresh token doesn't expire, so there is no browser login to repeat.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

try:  # load .env next to this file; real environment variables win
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except ImportError:
    pass

BASE_URL = os.environ.get("TASTY_BASE_URL", "https://api.tastyworks.com")
USER_AGENT = "qqq-payoff/1.0"  # tastytrade rejects requests without a User-Agent
CHAIN_TTL = 30 * 60            # strikes and expirations change rarely; cache them
MAX_SYMBOLS = 100              # per /market-data/by-type request, across all types


class RateLimited(RuntimeError):
    retry_later = True


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _price(item: dict):
    """Mark, else mid, else the bid/ask midpoint, else last."""
    for key in ("mark", "mid"):
        v = _num(item.get(key))
        if v is not None and v > 0:
            return v
    bid, ask = _num(item.get("bid")), _num(item.get("ask"))
    if bid is not None and ask:
        return (bid + ask) / 2
    return _num(item.get("last"))


def _key(symbol: str) -> str:
    return symbol.replace(" ", "")  # match OCC symbols however the padding comes back


def _ny_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # no tz database (e.g. Windows without tzdata): close enough
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date().isoformat()


class TastyClient:
    def __init__(self, client_secret: str | None = None, refresh_token: str | None = None,
                 base_url: str = BASE_URL):
        self.client_secret = client_secret or os.environ.get("TASTY_CLIENT_SECRET", "")
        self.refresh_token = refresh_token or os.environ.get("TASTY_REFRESH_TOKEN", "")
        self.base_url = base_url.rstrip("/")
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._token = None
        self._expires = 0.0
        self._lock = threading.Lock()
        self._chains: dict = {}
        self._spot: dict = {}

    # ---- auth ---------------------------------------------------------------------------
    def access_token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.time() >= self._expires:
                if not (self.client_secret and self.refresh_token):
                    raise RuntimeError("Set TASTY_CLIENT_SECRET and TASTY_REFRESH_TOKEN first.")
                r = self.http.post(f"{self.base_url}/oauth/token", timeout=20, json={
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_secret": self.client_secret,
                })
                if r.status_code != 200:
                    raise RuntimeError(f"tastytrade login failed ({r.status_code}): {r.text[:300]}")
                tok = r.json()
                self._token = tok["access_token"]
                self._expires = time.time() + int(tok.get("expires_in", 900)) - 60  # 15 min tokens
            return self._token

    def get(self, path: str, query: str = "") -> dict:
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        for attempt in range(2):  # on a 401, refresh the token once and retry
            token = self.access_token(force=attempt > 0)
            r = self.http.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            if r.status_code != 401:
                break
        if r.status_code == 429:
            raise RateLimited("tastytrade is rate-limiting requests (429); slow the refresh down")
        if not r.ok:
            raise RuntimeError(f"tastytrade {path} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    # ---- data ---------------------------------------------------------------------------
    def nested_chain(self, symbol: str) -> dict:
        hit = self._chains.get(symbol)
        if hit and time.time() - hit[0] < CHAIN_TTL:
            return hit[1]
        items = self.get(f"/option-chains/{quote(symbol, safe='')}/nested")["data"]["items"]
        if not items:
            raise RuntimeError(f"No option chain found for {symbol}")
        chain = next((c for c in items if c.get("option-chain-type") == "Standard"), items[0])
        self._chains[symbol] = (time.time(), chain)
        return chain

    def quotes(self, equities=(), options=()) -> dict:
        """REST quote snapshot keyed by symbol (spaces removed), batched 100 at a time."""
        wanted = [("equity", s) for s in equities] + [("equity-option", s) for s in options]
        out = {}
        for i in range(0, len(wanted), MAX_SYMBOLS):
            groups: dict = {}
            for kind, sym in wanted[i:i + MAX_SYMBOLS]:
                groups.setdefault(kind, []).append(sym)
            query = "&".join(f"{kind}={quote(','.join(syms), safe=',')}" for kind, syms in groups.items())
            for item in self.get("/market-data/by-type", query).get("data", {}).get("items", []):
                out[_key(item.get("symbol", ""))] = item
        return out

    def spot(self, symbol: str):
        item = self.quotes(equities=[symbol]).get(_key(symbol))
        price = _price(item) if item else None
        if price:
            self._spot[symbol] = price
        return price

    def expirations(self, symbol: str) -> dict:
        today = _ny_today()
        exps = sorted({e["expiration-date"] for e in self.nested_chain(symbol).get("expirations", [])
                       if e.get("expiration-date", "") >= today})
        return {"symbol": symbol, "spot": self.spot(symbol), "isDelayed": False,
                "source": "tastytrade", "expirations": exps}

    def chain(self, symbol: str, expiration: str, strikes: int = 60) -> dict:
        """Quotes for the `strikes` strikes nearest the money on one expiration.
        IV and greeks are left empty; the dashboard computes them from the mid prices."""
        exp = next((e for e in self.nested_chain(symbol).get("expirations", [])
                    if e.get("expiration-date") == expiration), None)
        if exp is None:
            raise RuntimeError(f"{symbol} has no {expiration} expiration")
        spot = self._spot.get(symbol) or self.spot(symbol)
        rows = [s for s in exp.get("strikes", []) if _num(s.get("strike-price")) is not None]
        if spot:
            rows.sort(key=lambda s: abs(_num(s["strike-price"]) - spot))
        rows = sorted(rows[:strikes], key=lambda s: _num(s["strike-price"]))
        option_symbols = [sym for s in rows for sym in (s.get("call"), s.get("put")) if sym]
        quotes = self.quotes(equities=[symbol], options=option_symbols)
        underlying = quotes.get(_key(symbol))
        if underlying and _price(underlying):
            spot = self._spot[symbol] = _price(underlying)

        contracts = []
        for s in rows:
            strike = _num(s["strike-price"])
            for side, sym in (("CALL", s.get("call")), ("PUT", s.get("put"))):
                if not sym:
                    continue
                q = quotes.get(_key(sym), {})
                contracts.append({
                    "symbol": sym, "putCall": side, "expiration": expiration,
                    "dte": exp.get("days-to-expiration"), "strike": strike,
                    "bid": _num(q.get("bid")), "ask": _num(q.get("ask")), "last": _num(q.get("last")),
                    "mark": _price(q) if q else None, "volume": _num(q.get("volume")),
                    "oi": _num(q.get("open-interest")),
                    "iv": None, "delta": None, "gamma": None, "theta": None, "vega": None,
                })
        return {"symbol": symbol, "expiration": expiration, "spot": spot, "isDelayed": False,
                "source": "tastytrade", "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "contracts": contracts}


def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "QQQ").upper()
    client = TastyClient()
    try:
        client.access_token()
        print("Logged in to tastytrade.")
        exps = client.expirations(symbol)
        print(f"{symbol} spot {exps['spot']}, {len(exps['expirations'])} expirations "
              f"({exps['expirations'][0]} to {exps['expirations'][-1]})")
        exp = next((e for e in exps["expirations"] if e > _ny_today()), exps["expirations"][0])
        chain = client.chain(symbol, exp, strikes=6)
        print(f"\n{exp}, 6 strikes nearest the money:")
        for c in chain["contracts"]:
            print(f"  {c['strike']:>8.2f} {c['putCall']:<4}  bid {c['bid']}  ask {c['ask']}  mark {c['mark']}")
    except Exception as err:
        sys.exit(f"Problem: {err}")


if __name__ == "__main__":
    main()
