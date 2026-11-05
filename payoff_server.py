#!/usr/bin/env python3
"""
payoff_server.py - local server for the option payoff dashboard (index.html).

Put payoff_server.py, index.html and the feed files in one folder.

    python payoff_server.py                        # Alpaca data, opens http://127.0.0.1:8765
    python payoff_server.py --source tastytrade    # tastytrade data (funded account)
    python payoff_server.py --source schwab        # Schwab data (approved Schwab app)
    python payoff_server.py --strikes 80 --port 9000 --no-browser

Credentials and settings come from environment variables or a .env file next to this
script (copy .env.example to .env). Alpaca needs ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY;
tastytrade needs TASTY_CLIENT_SECRET and TASTY_REFRESH_TOKEN; Schwab needs SCHWAB_APP_KEY
and SCHWAB_APP_SECRET.

By default the server only listens on 127.0.0.1. To deploy it, set HOST=0.0.0.0, PORT
(most hosts set this for you) and ALLOWED_HOSTS to your public domain. It only serves
market data, never your tokens.
Opening index.html directly (without this server) runs it on simulated prices.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:  # load .env next to this file; real environment variables win
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except ImportError:
    pass

INDEX = Path(__file__).resolve().with_name("index.html")
# Host headers the server answers to (blocks DNS rebinding). Add your deployed domain via env.
ALLOWED_HOSTS = {"127.0.0.1", "localhost"} | {
    h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
SYMBOL_RE = re.compile(r"^\$?[A-Z][A-Z./]{0,9}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CHAIN_TTL = 2.0          # seconds; several browser tabs share one upstream call
EXPIRATIONS_TTL = 300.0
BACKOFF = 15.0           # when rate-limited, keep serving the last snapshot this long

SOURCE = None            # set in main()
_lock = threading.Lock() # one upstream call at a time
_cache: dict = {}


class BadRequest(ValueError):
    pass


def _num(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


class AlpacaSource:
    name = "alpaca"

    def __init__(self, strikes: int = 60):
        import alpaca_feed
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
        import tasty_feed
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

    def __init__(self):
        import qqq_chain
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


def _symbol(query: dict) -> str:
    symbol = query.get("symbol", "QQQ").strip().upper()
    if not SYMBOL_RE.match(symbol):
        raise BadRequest(f"Not a valid symbol: {symbol!r}")
    return symbol


def _expiration(query: dict) -> str:
    exp = query.get("expiration", "")
    if not DATE_RE.match(exp):
        raise BadRequest("expiration must look like YYYY-MM-DD")
    return exp


class Handler(BaseHTTPRequestHandler):
    server_version = "PayoffServer/1.2"

    def log_message(self, fmt, *args):  # keep the terminal clear for login prompts
        pass

    def do_GET(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if "*" not in ALLOWED_HOSTS and host not in ALLOWED_HOSTS:  # blocks DNS-rebinding pages from reading the API
            return self._send(403, b"Forbidden", "text/plain")
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/expirations":
                symbol = _symbol(query)
                return self._json(_cached(("exp", symbol), EXPIRATIONS_TTL,
                                          lambda: SOURCE.expirations(symbol)))
            if url.path == "/api/chain":
                symbol, exp = _symbol(query), _expiration(query)
                return self._json(_cached(("chain", symbol, exp), CHAIN_TTL,
                                          lambda: SOURCE.chain(symbol, exp)))
            self._json({"error": "Not found"}, 404)
        except BadRequest as err:
            self._json({"error": str(err), "source": SOURCE.name}, 400)
        except Exception as err:  # upstream or network trouble: tell the page what happened
            print(f"[{datetime.now():%H:%M:%S}] {SOURCE.name}: {type(err).__name__}: {err}")
            self._json({"error": str(err), "source": SOURCE.name}, 502)

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj, allow_nan=False).encode(), "application/json")

    def _send(self, status, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    global SOURCE
    ap = argparse.ArgumentParser(description="Serve the option payoff dashboard with live data.")
    ap.add_argument("--source", choices=["alpaca", "tastytrade", "schwab"],
                    default=os.environ.get("DATA_SOURCE", "alpaca"))
    ap.add_argument("--strikes", type=int, default=int(os.environ.get("STRIKES", "60")),
                    help="Alpaca/tastytrade: strikes per expiration, nearest the money (default 60)")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                    help="interface to listen on (default 127.0.0.1; use 0.0.0.0 when deployed)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()
    if not INDEX.exists():
        raise SystemExit(f"index.html not found - put it next to {Path(__file__).name}")

    SOURCE = {"alpaca": lambda: AlpacaSource(args.strikes), "tastytrade": lambda: TastySource(args.strikes),
              "schwab": SchwabSource}[args.source]()
    try:
        SOURCE.check()
    except Exception as err:
        raise SystemExit(f"Couldn't connect to {SOURCE.name}: {err}")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    local = args.host in ("127.0.0.1", "localhost")
    url = f"http://{'127.0.0.1' if local or args.host == '0.0.0.0' else args.host}:{args.port}"
    print(f"Payoff dashboard ({SOURCE.name} data) on {args.host}:{args.port} - {url}   (Ctrl+C to stop)",
          flush=True)
    if local and not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
