"""Tests run against TEST_DATABASE_URL (a separate Postgres database that gets wiped)."""
import os

from dotenv import dotenv_values

_env = dotenv_values(os.path.join(os.path.dirname(__file__), "..", ".env"))
TEST_DB = os.environ.get("TEST_DATABASE_URL") or _env.get("TEST_DATABASE_URL")
if not TEST_DB:
    raise RuntimeError("Set TEST_DATABASE_URL (a throwaway database) to run the tests.")
os.environ["DATABASE_URL"] = TEST_DB          # must happen before app modules read settings
os.environ["ALLOWED_HOSTS"] = "testserver"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.db import get_engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import auth, market  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
HDR = {"X-Requested-With": "payoff"}


@pytest.fixture(scope="session", autouse=True)
def schema():
    cfg = Config(os.path.join(ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(ROOT, "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean():
    with get_engine().begin() as conn:
        conn.execute(text("TRUNCATE strategies, sessions, users CASCADE"))
    for limiter in (auth.login_by_email, auth.login_by_ip, auth.signup_by_ip):
        limiter._hits.clear()
    yield


class FakeSource:
    name = "fake"

    def check(self):
        pass

    def expirations(self, symbol):
        return {"symbol": symbol, "spot": 600.0, "source": "fake", "expirations": ["2026-11-20"]}

    def chain(self, symbol, expiration):
        return {"symbol": symbol, "expiration": expiration, "spot": 600.0, "source": "fake", "contracts": []}


@pytest.fixture
def client():
    market.set_source(FakeSource())
    with TestClient(create_app(connect_source=False)) as c:
        yield c


def signup(c, email="a@example.com", password="correct horse battery"):
    r = c.post("/api/auth/signup", json={"email": email, "password": password}, headers=HDR)
    assert r.status_code == 201, r.text
    return r
