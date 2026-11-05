# QQQ Payoff Dashboard

An option payoff dashboard (`index.html`) served by a small Python server
(`payoff_server.py`) that pulls live option chains from Alpaca, tastytrade or Schwab.
Opening `index.html` directly, with no server, runs it on simulated prices.

## Files

| File | Purpose |
| --- | --- |
| `index.html` | The dashboard UI |
| `payoff_server.py` | Serves the UI and `/api/expirations`, `/api/chain` |
| `alpaca_feed.py` | Alpaca market data client (default source) |
| `tasty_feed.py` | tastytrade client |
| `qqq_chain.py` | Schwab Trader API client (also a standalone CLI) |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your keys
python payoff_server.py     # opens http://127.0.0.1:8765
```

Options: `--source alpaca|tastytrade|schwab`, `--strikes 80`, `--port 9000`, `--no-browser`.
Each also has an env var (`DATA_SOURCE`, `STRIKES`, `PORT`); see `.env.example`.

## Configuration and secrets

All credentials are read from environment variables, or from a `.env` file next to the
scripts. Real environment variables take precedence over `.env`.

- `.env` holds your secrets and is git-ignored. Never commit it.
- `.env.example` lists every variable with blank values and is safe to commit.
- `schwab_tokens.json` (created by the Schwab login) grants brokerage access and is git-ignored.

## Deploy (Render, Railway, Heroku-style hosts)

The repo includes a `Procfile` and a `render.yaml`. On the host:

1. Create a web service from this GitHub repo.
   - Build: `pip install -r requirements.txt`
   - Start: `python payoff_server.py --host 0.0.0.0 --no-browser`
2. Set environment variables in the host's dashboard (not in the repo):
   - `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`
   - `ALLOWED_HOSTS=your-app.onrender.com` (your public hostname; requests with any other
     `Host` header get a 403)
   - `PORT` is usually set by the host automatically.

Notes:

- Use the **Alpaca** or **tastytrade** source when deploying. Schwab needs an
  interactive browser login in a terminal, so it only works locally.
- The deployed API has no login. Anyone with the URL can view the dashboard and use your
  data-feed quota. Keep the URL private for a prototype, or put it behind your host's
  access controls.

## GitHub Pages (static demo only)

GitHub Pages can't run Python, but it can host `index.html` on its own, which runs on
simulated prices. Enable Pages on the repo's root to get a demo link.
