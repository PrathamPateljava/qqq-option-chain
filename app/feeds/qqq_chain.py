#!/usr/bin/env python3
"""
qqq_chain.py - pull a live option chain (default QQQ) from Schwab's Trader API into pandas.

One-time setup
  1. pip install -r requirements.txt
  2. Put your app credentials (developer.schwab.com -> Dashboard -> your app) in .env
     as SCHWAB_APP_KEY and SCHWAB_APP_SECRET (see .env.example).
     The callback URL registered on your app must match CALLBACK_URL exactly
     (default https://127.0.0.1 - override with SCHWAB_CALLBACK_URL).
  3. python -m app.feeds.qqq_chain
     The first run prints a login URL. After you approve, the browser lands on a page
     that fails to load - that's expected. Copy the FULL URL from the address bar and
     paste it into the terminal right away (the code in it expires quickly).

Tokens are cached in schwab_tokens.json. They grant access to your brokerage account,
so keep that file private and out of git. Access tokens auto-refresh every 30 minutes;
Schwab forces a fresh browser login every 7 days.

Usage
  python -m app.feeds.qqq_chain                                  # expiries in the next 7 days, 30 strikes
  python -m app.feeds.qqq_chain --days 30 --strikes 60 --out qqq.csv
  python -m app.feeds.qqq_chain --watch 15 --out qqq_ticks.csv   # poll every 15s, append snapshots

In a notebook:
  from app.feeds.qqq_chain import fetch_chain, chain_to_df
  df = chain_to_df(fetch_chain("QQQ", days=7, strikes=30))
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import webbrowser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests

try:  # load the project's .env; real environment variables win
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

APP_KEY = os.environ.get("SCHWAB_APP_KEY", "")
APP_SECRET = os.environ.get("SCHWAB_APP_SECRET", "")
CALLBACK_URL = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
TOKEN_FILE = Path(os.environ.get("SCHWAB_TOKEN_FILE", "schwab_tokens.json"))

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
CHAINS_URL = "https://api.schwabapi.com/marketdata/v1/chains"

REFRESH_TTL = 7 * 24 * 3600  # refresh-token lifetime (forces a browser login weekly)
EARLY = 60                   # refresh the access token this many seconds before expiry


# ------------------------------------------------------------------ auth --
def _token_request(data: dict) -> dict:
    basic = base64.b64encode(f"{APP_KEY}:{APP_SECRET}".encode()).decode()
    r = requests.post(
        TOKEN_URL,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=data,
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Token request failed ({r.status_code}): {r.text[:300]}")
    return r.json()


def _save_tokens(tok: dict, refresh_issued_at: float) -> dict:
    tok["access_expires_at"] = time.time() + int(tok.get("expires_in", 1800)) - EARLY
    tok["refresh_issued_at"] = refresh_issued_at
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return tok


def _browser_login() -> dict:
    url = f"{AUTH_URL}?client_id={APP_KEY}&redirect_uri={CALLBACK_URL}"
    print(f"\n1) Log in to Schwab and approve access:\n\n   {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    pasted = input("2) Paste the full URL you were redirected to:\n> ").strip()
    code = parse_qs(urlparse(pasted).query).get("code", [""])[0]  # also decodes %40 -> @
    if not code:
        sys.exit("No ?code= found in that URL - run the script again.")
    tok = _token_request({"grant_type": "authorization_code",
                          "code": code,
                          "redirect_uri": CALLBACK_URL})
    print("Logged in. Tokens saved to", TOKEN_FILE)
    return _save_tokens(tok, refresh_issued_at=time.time())


def get_access_token(force_refresh: bool = False) -> str:
    if not (APP_KEY and APP_SECRET):
        sys.exit("Set SCHWAB_APP_KEY and SCHWAB_APP_SECRET environment variables first.")
    if not TOKEN_FILE.exists():
        return _browser_login()["access_token"]

    tok = json.loads(TOKEN_FILE.read_text())
    now = time.time()
    if now - tok.get("refresh_issued_at", 0) > REFRESH_TTL - 3600:
        print("Refresh token is ~7 days old - Schwab requires a new login.")
        return _browser_login()["access_token"]
    if force_refresh or now >= tok.get("access_expires_at", 0):
        try:
            new = _token_request({"grant_type": "refresh_token",
                                  "refresh_token": tok["refresh_token"]})
        except RuntimeError as err:
            print(f"{err}\nFalling back to a fresh login.")
            return _browser_login()["access_token"]
        new.setdefault("refresh_token", tok["refresh_token"])  # works whether or not it rotates
        tok = _save_tokens(new, refresh_issued_at=tok["refresh_issued_at"])
    return tok["access_token"]


# ----------------------------------------------------------------- chain --
def fetch_chain(symbol: str = "QQQ", days: int = 7, strikes: int | None = 30,
                from_date: str | None = None, to_date: str | None = None) -> dict:
    """Raw /chains JSON. Keep the window narrow: QQQ's full chain is enormous.
    strikes=None returns every strike; from_date/to_date (YYYY-MM-DD) override `days`."""
    today = date.today()
    params = {
        "symbol": symbol,
        "contractType": "ALL",
        "includeUnderlyingQuote": "true",
        "strategy": "SINGLE",
        "fromDate": from_date or today.isoformat(),
        "toDate": to_date or (today + timedelta(days=days)).isoformat(),
    }
    if strikes:
        params["strikeCount"] = strikes    # strikes around the money, per expiration
    for attempt in range(2):               # on a 401, force one token refresh and retry
        headers = {"Authorization": f"Bearer {get_access_token(force_refresh=attempt > 0)}",
                   "Accept": "application/json"}
        r = requests.get(CHAINS_URL, params=params, headers=headers, timeout=30)
        if r.status_code != 401:
            break
    if not r.ok:
        raise RuntimeError(f"Chain request failed ({r.status_code}): {r.text[:500]}")
    chain = r.json()
    if chain.get("status") != "SUCCESS":
        raise RuntimeError(f"Schwab returned status={chain.get('status')!r}")
    return chain


COLUMNS = ["putCall", "symbol", "expiration", "dte", "strikePrice", "bid", "ask", "last",
           "mark", "bidSize", "askSize", "totalVolume", "openInterest", "volatility",
           "delta", "gamma", "theta", "vega", "rho", "theoreticalOptionValue",
           "inTheMoney", "quoteTimeInLong"]
NUMERIC = ["strikePrice", "bid", "ask", "last", "mark", "bidSize", "askSize", "totalVolume",
           "openInterest", "volatility", "delta", "gamma", "theta", "vega", "rho",
           "theoreticalOptionValue"]
GREEKS = ["volatility", "delta", "gamma", "theta", "vega", "rho"]


def chain_to_df(chain: dict) -> pd.DataFrame:
    """Flatten callExpDateMap / putExpDateMap into one row per contract."""
    rows = []
    for side in ("callExpDateMap", "putExpDateMap"):
        for exp_key, by_strike in (chain.get(side) or {}).items():
            exp, _, dte = exp_key.partition(":")        # keys look like "2026-10-30:3"
            for contracts in by_strike.values():
                for c in contracts:
                    rows.append({**c, "expiration": exp,
                                 "dte": int(dte) if dte.isdigit() else None})
    if not rows:
        return pd.DataFrame(columns=COLUMNS)

    df = pd.DataFrame(rows).reindex(columns=COLUMNS)
    df[NUMERIC] = df[NUMERIC].apply(pd.to_numeric, errors="coerce")
    df[GREEKS] = df[GREEKS].mask(df[GREEKS] <= -999)    # guard against "not computed" sentinels
    df["iv"] = df.pop("volatility") / 100               # Schwab reports IV in percent
    df["expiration"] = pd.to_datetime(df["expiration"]).dt.date
    df["quote_time"] = (pd.to_datetime(df.pop("quoteTimeInLong"), unit="ms", utc=True)
                          .dt.tz_convert("America/New_York"))
    df["underlying_price"] = chain.get("underlyingPrice")
    df["is_delayed"] = chain.get("isDelayed")
    return df.sort_values(["expiration", "strikePrice", "putCall"]).reset_index(drop=True)


def print_summary(df: pd.DataFrame, chain: dict, n: int = 10) -> None:
    spot = chain.get("underlyingPrice")
    print(f"\n{chain.get('symbol')}  spot={spot}  contracts={len(df)}  "
          f"delayed={chain.get('isDelayed')}  at {datetime.now():%H:%M:%S}")
    if chain.get("isDelayed"):
        print("  ! Schwab flagged this data as delayed - check your account's quote settings.")
    if df.empty:
        print("  No contracts returned - widen --days or check the symbol.")
        return
    near = df[df["expiration"] == df["expiration"].min()]
    cols = ["bid", "ask", "iv", "delta", "openInterest"]

    def side(pc: str) -> pd.DataFrame:
        legs = near[near["putCall"] == pc].drop_duplicates("strikePrice")
        return legs.set_index("strikePrice")[cols]

    board = side("CALL").join(side("PUT"), lsuffix="_c", rsuffix="_p", how="outer")
    center = spot if spot is not None else board.index.to_series().median()
    closest = (board.index.to_series() - center).abs().nsmallest(n).index
    print(f"  Nearest expiry {near['expiration'].iloc[0]} (calls _c | puts _p):")
    print(board.loc[closest].sort_index().round(3).to_string())


# ------------------------------------------------------------------ main --
def main() -> None:
    ap = argparse.ArgumentParser(description="Live option chain from Schwab into pandas.")
    ap.add_argument("--symbol", default="QQQ")
    ap.add_argument("--days", type=int, default=7, help="expirations up to N days out (default 7)")
    ap.add_argument("--strikes", type=int, default=30, help="strikes around the money (default 30)")
    ap.add_argument("--out", help="write to CSV (appends snapshots in --watch mode)")
    ap.add_argument("--watch", type=float, metavar="SEC", help="re-poll every SEC seconds")
    args = ap.parse_args()

    try:
        while True:
            chain = fetch_chain(args.symbol, args.days, args.strikes)
            df = chain_to_df(chain)
            df.insert(0, "snapshot_utc", datetime.now(timezone.utc))
            print_summary(df, chain)
            if args.out:
                append = bool(args.watch) and Path(args.out).exists()
                df.to_csv(args.out, mode="a" if append else "w", header=not append, index=False)
            if not args.watch:
                break
            time.sleep(max(args.watch, 1.0))   # Schwab allows ~120 requests/min
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
