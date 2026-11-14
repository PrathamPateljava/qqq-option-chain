# QQQ Payoff Dashboard

An option payoff dashboard with live chains from Alpaca, tastytrade or Schwab, plus user
accounts so people can save strategies and come back to them.

- **Without an account:** build and analyse strategies as usual. Nothing is stored; positions
  are gone when the page closes.
- **With an account:** Save, Save as new, **My strategies** (open, rename, delete) and notes.
  Strategies are stored in Postgres and only their owner can see them.

## Layout

| Path | Purpose |
| --- | --- |
| `static/index.html` | The dashboard UI |
| `app/main.py` | FastAPI app: serves the page and mounts the API |
| `app/routers/auth.py` | `/api/auth/signup`, `login`, `logout`, `me`, `forgot`, `reset` |
| `app/mailer.py` | Outgoing email (printed locally, sent through Brevo when deployed) |
| `app/routers/strategies.py` | `/api/strategies` list / create / get / update / delete |
| `app/routers/market.py` | `/api/expirations`, `/api/chain` (market data) |
| `app/feeds/` | Alpaca, tastytrade and Schwab clients |
| `app/models.py`, `alembic/` | Database tables and migrations |
| `app/cli.py` | Admin commands (list users, reset a password, delete a user) |
| `tests/` | API tests (run against a separate test database) |

## Run locally

You need Python 3.12 and PostgreSQL (`brew install postgresql@17 && brew services start postgresql@17`).

```bash
# one-time database setup (pick your own password)
/opt/homebrew/opt/postgresql@17/bin/psql -d postgres -c "CREATE ROLE payoff LOGIN PASSWORD 'change-me';"
/opt/homebrew/opt/postgresql@17/bin/createdb -O payoff payoff
/opt/homebrew/opt/postgresql@17/bin/createdb -O payoff payoff_test

python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # fill in DATABASE_URL, TEST_DATABASE_URL and your data-feed keys
alembic upgrade head          # create the tables
python -m app                 # opens http://127.0.0.1:8765
```

Options: `python -m app --source alpaca|tastytrade|schwab --strikes 80 --port 9000 --no-browser`.
Each also has an env var (`DATA_SOURCE`, `STRIKES`, `PORT`).

Tests: `pytest` (uses `TEST_DATABASE_URL`, which it wipes).

## Accounts and security

- Passwords are hashed with Argon2. Logins are server-side sessions in an HttpOnly,
  SameSite=Lax cookie (Secure when `COOKIE_SECURE=true`). Logging out deletes the session.
- Failed logins are rate-limited per account and per IP. Writes require an
  `X-Requested-With` header and a same-site Origin (CSRF protection).
- Every strategy query is scoped to the logged-in user; another user's strategy returns 404.

### Password resets

**Forgot password?** on the login screen emails a link that works once and expires in an hour.
Choosing a new password logs the account out on every other device and logs it in here.
The reply never says whether an email has an account, only the hash of each link is stored,
and each address can request 3 emails an hour.

Locally (`EMAIL_BACKEND=console`) the email, link included, is printed in the terminal running
the server instead of being sent.

An admin can still reset a password by hand from a machine with `DATABASE_URL` set:

```bash
python -m app.cli reset-password someone@example.com --generate   # prints a temporary password
python -m app.cli list-users
python -m app.cli delete-user someone@example.com
python -m app.cli send-test-email you@example.com                  # checks the email settings
```

A manual reset also logs that user out everywhere. Send the temporary password over a channel you trust.

### Email (Brevo)

Render's free plan blocks SMTP, so email goes through Brevo's HTTPS API. Brevo's free plan sends
300 emails a day and works without your own domain:

1. Sign up at brevo.com. Under **Senders, domains & dedicated IPs > Senders**, add the address
   to send from (your Gmail is fine) and confirm it with the code Brevo emails you.
2. Under **SMTP & API > API keys**, create a key.
3. Set `EMAIL_BACKEND=brevo`, `EMAIL_FROM=<that address>` and `BREVO_API_KEY=<the key>`.
   Check with `python -m app.cli send-test-email you@example.com`.

Brevo sends mail from a Gmail sender through its own address (yourname@...t-sender-sib.com), so
some emails may land in spam. Authenticating a domain you own in Brevo fixes that.
Brevo also learns which IP addresses use your key and, after 30 days without a new one, blocks
unknown ones. If emails stop with a 401 in the logs, approve the address under **Security >
Authorized IPs**.

## Configuration and secrets

Everything comes from environment variables or `.env` (git-ignored; real env vars win).
`.env.example` lists every setting with blank values and is safe to commit.

## Deploy (Render + Neon, both free)

1. Create a free Postgres database on **neon.tech** and copy its connection string.
2. On **render.com**: New > Blueprint > this repo. `render.yaml` creates the web service; it
   runs `alembic upgrade head` on every start. Set these in Render's dashboard:
   - `DATABASE_URL`: the Neon connection string
   - `APP_URL`: your site's address, e.g. `https://qqq-payoff.onrender.com` (its hostname is
     allowed automatically, so `ALLOWED_HOSTS` is only needed for extra hostnames)
   - `EMAIL_FROM`, `BREVO_API_KEY`: see [Email (Brevo)](#email-brevo)
   - `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`
   - `COOKIE_SECURE=true` and `EMAIL_BACKEND=brevo` are already set in the blueprint.
3. To reset a password in production, run the CLI locally with `DATABASE_URL` pointing at Neon.

Notes:
- Use the Alpaca or tastytrade source when deployed; Schwab needs an interactive terminal login.
- Login rate limits are kept in memory, so they assume a single server instance.
- Market data has no login of its own: anyone with the URL can view prices and use your feed quota.
