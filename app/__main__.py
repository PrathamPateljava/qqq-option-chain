"""Run the dashboard: python -m app [--source alpaca|tastytrade|schwab] [--port 8765] [--no-browser]

Every option can also come from .env (DATA_SOURCE, STRIKES, HOST, PORT); flags win.
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the option payoff dashboard.")
    ap.add_argument("--source", choices=["alpaca", "tastytrade", "schwab"])
    ap.add_argument("--strikes", type=int, help="strikes per expiration, nearest the money")
    ap.add_argument("--host", help="interface to listen on (127.0.0.1 locally, 0.0.0.0 when deployed)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = ap.parse_args()
    for flag, env in (("source", "DATA_SOURCE"), ("strikes", "STRIKES"), ("host", "HOST"), ("port", "PORT")):
        if getattr(args, flag) is not None:
            os.environ[env] = str(getattr(args, flag))

    import uvicorn

    from app.config import get_settings
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    local = settings.host in ("127.0.0.1", "localhost")
    url = f"http://127.0.0.1:{settings.port}"
    print(f"Payoff dashboard ({settings.data_source} data) on {settings.host}:{settings.port}"
          + (f" - {url}" if local else ""), flush=True)
    if local and not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, proxy_headers=True,
                log_level="warning")


if __name__ == "__main__":
    main()
