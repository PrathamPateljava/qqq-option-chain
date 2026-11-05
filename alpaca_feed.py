#!/usr/bin/env python3
"""
alpaca_feed.py - option chain snapshots from Alpaca's market data API.

One-time setup
  1. Sign up at alpaca.markets. A free paper-trading account is enough: no deposit,
     no brokerage account.
  2. In the paper-trading dashboard, generate an API key (key ID + secret key).
  3. Put ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY in .env (see .env.example)
     or export them as environment variables.

Check your setup:   python alpaca_feed.py QQQ

Feeds: the free plan gets Alpaca's indicative options feed, where quotes are modified
and trades delayed, so treat prices as approximate. With the Algo Trader Plus
subscription, set ALPACA_OPTIONS_FEED=opra for real exchange quotes (and
ALPACA_STOCK_FEED=sip for the consolidated stock price).
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

try:  # load .env next to this file; real environment variables win
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except ImportError:
    pass

DATA_URL = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
OPTIONS_FEED = os.environ.get("ALPACA_OPTIONS_FEED", "indicative")
STOCK_FEED = os.environ.get("ALPACA_STOCK_FEED", "iex")
SPOT_TTL = 2.0   # seconds; one stock snapshot serves every expiration in a refresh
WINDOW = 0.15    # fetch strikes within +/-15% of spot, then keep the nearest N


class RateLimited(RuntimeError):
    retry_later = True


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _ny_today() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    except Exception:  # no tz database (e.g. Windows without tzdata): close enough
        return (datetime.now(timezone.utc) - timedelta(hours=5)).date().isoformat()


def parse_occ(symbol: str):
    """'QQQ261120C00600000' -> root, expiration, type, strike and the space-padded OCC symbol."""
    s = symbol.replace(" ", "")
    if len(s) < 16:
        return None
    root, ymd, cp, strike = s[:-15], s[-15:-9], s[-9], s[-8:]
    if cp not in ("C", "P") or not (ymd.isdigit() and strike.isdigit()):
        return None
    return {"root": root, "expiration": f"20{ymd[:2]}-{ymd[2:4]}-{ymd[4:]}", "type": cp,
            "strike": int(strike) / 1000, "occ": f"{root:<6}{ymd}{cp}{strike}"}


class AlpacaClient:
    def __init__(self, key_id: str | None = None, secret_key: str | None = None,
                 data_url: str = DATA_URL, options_feed: str = OPTIONS_FEED, stock_feed: str = STOCK_FEED):
        self.key_id = key_id or os.environ.get("ALPACA_API_KEY_ID", "")
        self.secret_key = secret_key or os.environ.get("ALPACA_API_SECRET_KEY", "")
        self.data_url = data_url.rstrip("/")
        self.options_feed = options_feed
        self.stock_feed = stock_feed
        self.http = requests.Session()
        self.http.headers.update({
            "APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json", "User-Agent": "qqq-payoff/1.0",
        })
        self._spot: dict = {}

    @property
    def quality(self) -> str:
        return "indicative" if self.options_feed == "indicative" else "live"

    def get(self, path: str, params: dict) -> dict:
        if not (self.key_id and self.secret_key):
            raise RuntimeError("Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY first.")
        r = self.http.get(f"{self.data_url}{path}", params=params, timeout=20)
        if r.status_code == 429:
            raise RateLimited("Alpaca is rate-limiting requests (429); slow the refresh down")
        if r.status_code in (401, 403):
            raise RuntimeError(f"Alpaca refused the request ({r.status_code}): {r.text[:200]}")
        if not r.ok:
            raise RuntimeError(f"Alpaca {path} failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    def check(self, symbol: str = "QQQ"):
        if self.spot(symbol) is None:
            raise RuntimeError(f"Alpaca returned no price for {symbol}")

    def spot(self, symbol: str):
        hit = self._spot.get(symbol)
        if hit and time.time() - hit[0] < SPOT_TTL:
            return hit[1]
        snap = self.get(f"/v2/stocks/{quote(symbol, safe='')}/snapshot", {"feed": self.stock_feed})
        q, t = snap.get("latestQuote") or {}, snap.get("latestTrade") or {}
        bid, ask, last = _num(q.get("bp")), _num(q.get("ap")), _num(t.get("p"))
        if bid and ask and ask >= bid and (ask - bid) / ask < 0.002:
            price = (bid + ask) / 2                     # tight quote: use the midpoint
        else:
            price = last or _num((snap.get("dailyBar") or {}).get("c")) \
                or _num((snap.get("prevDailyBar") or {}).get("c"))
        if price:
            self._spot[symbol] = (time.time(), price)
        return price

    def snapshots(self, underlying: str, **filters) -> dict:
        params = {"feed": self.options_feed, "limit": 1000}
        params.update({k: v for k, v in filters.items() if v is not None})
        out: dict = {}
        for _ in range(25):  # page cap, about 25,000 contracts
            data = self.get(f"/v1beta1/options/snapshots/{quote(underlying, safe='')}", params)
            out.update(data.get("snapshots") or {})
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return out

    def expirations(self, symbol: str, days: int = 180) -> dict:
        spot = self.spot(symbol)
        today = _ny_today()
        params = {"type": "call", "expiration_date_gte": today,
                  "expiration_date_lte": (date.fromisoformat(today) + timedelta(days=days)).isoformat()}
        if spot:  # a thin band of strikes is enough to see every expiration
            params.update(strike_price_gte=round(spot * 0.985, 2), strike_price_lte=round(spot * 1.015, 2))
        exps = sorted({p["expiration"] for p in map(parse_occ, self.snapshots(symbol, **params)) if p})
        return {"symbol": symbol, "spot": spot, "isDelayed": False, "quality": self.quality,
                "source": "alpaca", "expirations": exps}

    def chain(self, symbol: str, expiration: str, strikes: int = 60) -> dict:
        spot = self.spot(symbol)
        filters = {"expiration_date": expiration}
        if spot:
            filters.update(strike_price_gte=round(spot * (1 - WINDOW), 2),
                           strike_price_lte=round(spot * (1 + WINDOW), 2))
        parsed = []
        for sym, snap in self.snapshots(symbol, **filters).items():
            p = parse_occ(sym)
            if p and p["expiration"] == expiration:
                parsed.append((p, snap))
        if any(p["root"] == symbol for p, _ in parsed):  # skip adjusted roots such as QQQ1
            parsed = [(p, s) for p, s in parsed if p["root"] == symbol]
        keep = sorted({p["strike"] for p, _ in parsed}, key=lambda k: abs(k - spot) if spot else k)[:strikes]
        keep = set(keep)
        dte = (date.fromisoformat(expiration) - date.fromisoformat(_ny_today())).days

        contracts = []
        for p, snap in sorted(parsed, key=lambda ps: (ps[0]["strike"], ps[0]["type"])):
            if p["strike"] not in keep:
                continue
            q, t, g = snap.get("latestQuote") or {}, snap.get("latestTrade") or {}, snap.get("greeks") or {}
            bid, ask, last = _num(q.get("bp")), _num(q.get("ap")), _num(t.get("p"))
            iv = _num(snap.get("impliedVolatility"))
            contracts.append({
                "symbol": p["occ"], "putCall": "CALL" if p["type"] == "C" else "PUT",
                "expiration": expiration, "dte": dte, "strike": p["strike"],
                "bid": bid, "ask": ask, "last": last,
                "mark": (bid + ask) / 2 if bid is not None and ask and ask >= bid else last,
                "volume": None, "oi": None,
                "iv": iv if iv and iv > 0 else None,
                "delta": _num(g.get("delta")), "gamma": _num(g.get("gamma")),
                "theta": _num(g.get("theta")), "vega": _num(g.get("vega")),
            })
        return {"symbol": symbol, "expiration": expiration, "spot": spot, "isDelayed": False,
                "quality": self.quality, "source": "alpaca",
                "asOf": datetime.now(timezone.utc).isoformat(timespec="seconds"), "contracts": contracts}


def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "QQQ").upper()
    client = AlpacaClient()
    try:
        client.check(symbol)
        exps = client.expirations(symbol)
        print(f"Connected to Alpaca ({client.options_feed} options feed, {client.stock_feed} stock feed).")
        print(f"{symbol} spot {exps['spot']:.2f}, {len(exps['expirations'])} expirations "
              f"({exps['expirations'][0]} to {exps['expirations'][-1]})")
        exp = next((e for e in exps["expirations"] if e > _ny_today()), exps["expirations"][0])
        print(f"\n{exp}, 6 strikes nearest the money:")
        for c in client.chain(symbol, exp, strikes=6)["contracts"]:
            iv = f"{c['iv'] * 100:.1f}%" if c["iv"] else "n/a"
            print(f"  {c['strike']:>8.2f} {c['putCall']:<4}  bid {c['bid']}  ask {c['ask']}  IV {iv}")
    except Exception as err:
        sys.exit(f"Problem: {err}")


if __name__ == "__main__":
    main()
