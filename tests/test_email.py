import re

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import mailer
from app.config import Settings
from app.routers import auth
from app.db import get_engine
from app.main import create_app
from tests.conftest import HDR, signup

PASSWORD = "correct horse battery"
NEW_PASSWORD = "a brand new passphrase"


def _forgot(client, email="a@example.com"):
    r = client.post("/api/auth/forgot", json={"email": email}, headers=HDR)
    assert r.status_code == 202, r.text
    return r


def _token(msg):
    m = re.search(r"/#reset=([\w-]+)", msg.text)
    assert m, msg.text
    assert m.group(0) in msg.html
    return m.group(1)


def _login(client, password, email="a@example.com"):
    return client.post("/api/auth/login", json={"email": email, "password": password}, headers=HDR)


def test_reset_flow(client, outbox):
    signup(client)
    client.cookies.clear()
    assert _forgot(client).json() == {"ok": True}
    assert len(outbox) == 1 and outbox[0].to == "a@example.com"
    assert "http://testserver/#reset=" in outbox[0].text  # no APP_URL: the address it was opened at
    token = _token(outbox[0])

    # A too-short password is refused without using up the link.
    r = client.post("/api/auth/reset", json={"token": token, "password": "short"}, headers=HDR)
    assert r.status_code == 422
    r = client.post("/api/auth/reset", json={"token": token, "password": NEW_PASSWORD}, headers=HDR)
    assert r.status_code == 200 and r.json() == {"email": "a@example.com"}
    assert client.get("/api/auth/me").status_code == 200  # logged in by the reset

    client.cookies.clear()
    assert _login(client, PASSWORD).status_code == 401
    assert _login(client, NEW_PASSWORD).status_code == 200

    # Each link works once.
    r = client.post("/api/auth/reset", json={"token": token, "password": "yet another password"}, headers=HDR)
    assert r.status_code == 400 and "Ask for a new one" in r.json()["error"]


def test_app_url_used_for_links(client, outbox, monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: _settings(APP_URL="https://payoff.example/"))
    signup(client)
    _forgot(client)
    assert "https://payoff.example/#reset=" in outbox[0].text


def test_unknown_email_gets_same_reply_and_no_email(client, outbox):
    assert _forgot(client, "nobody@example.com").json() == {"ok": True}
    assert outbox == []


def test_email_lookup_ignores_case(client, outbox):
    signup(client)
    _forgot(client, "A@Example.COM")
    assert [m.to for m in outbox] == ["a@example.com"]


def test_reset_logs_out_other_devices(client, outbox):
    signup(client)                       # this browser stays logged in with the old password
    with TestClient(create_app(connect_source=False)) as other:
        _forgot(other)
        r = other.post("/api/auth/reset", json={"token": _token(outbox[0]), "password": NEW_PASSWORD}, headers=HDR)
        assert r.status_code == 200
        assert other.get("/api/auth/me").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_any_link_works_and_then_all_are_used_up(client, outbox):
    signup(client)
    _forgot(client)
    _forgot(client)
    first, second = _token(outbox[0]), _token(outbox[1])
    assert client.post("/api/auth/reset", json={"token": first, "password": NEW_PASSWORD},
                       headers=HDR).status_code == 200
    assert client.post("/api/auth/reset", json={"token": second, "password": NEW_PASSWORD},
                       headers=HDR).status_code == 400


def test_expired_link_refused(client, outbox):
    signup(client)
    _forgot(client)
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE email_tokens SET expires_at = now() - interval '1 minute'"))
    r = client.post("/api/auth/reset", json={"token": _token(outbox[0]), "password": NEW_PASSWORD}, headers=HDR)
    assert r.status_code == 400


def test_tokens_stored_hashed(client, outbox):
    signup(client)
    _forgot(client)
    with get_engine().connect() as conn:
        stored = conn.execute(text("select id from email_tokens")).scalar_one()
    assert stored != _token(outbox[0]) and len(stored) == 64


def test_bad_tokens_refused(client):
    for token in ("nope", "x" * 200):
        r = client.post("/api/auth/reset", json={"token": token, "password": NEW_PASSWORD}, headers=HDR)
        assert r.status_code == 400
    r = client.post("/api/auth/reset", json={"token": "x" * 201, "password": NEW_PASSWORD}, headers=HDR)
    assert r.status_code == 422


def test_forgot_rate_limited_per_address(client, outbox):
    signup(client)
    for _ in range(3):
        _forgot(client)
    r = client.post("/api/auth/forgot", json={"email": "a@example.com"}, headers=HDR)
    assert r.status_code == 429 and len(outbox) == 3


def test_forgot_needs_csrf_header(client, outbox):
    assert client.post("/api/auth/forgot", json={"email": "a@example.com"}).status_code == 403


def test_send_failure_is_logged_not_raised(client, monkeypatch, caplog):
    def boom(_msg):
        raise mailer.EmailError("provider down")
    monkeypatch.setattr(mailer, "send", boom)
    signup(client)
    assert _forgot(client).status_code == 202
    assert "provider down" in caplog.text


# ---- backends ------------------------------------------------------------------------------
def _settings(**env):
    return Settings(_env_file=None, **env)


def test_brevo_request(monkeypatch):
    calls = []

    class Ok:
        status_code, text = 201, '{"messageId": "<1@x>"}'

    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)) or Ok())
    s = _settings(EMAIL_BACKEND="brevo", BREVO_API_KEY="xkeysib-test", EMAIL_FROM="me@gmail.com")
    mailer._brevo(mailer.password_reset("you@example.com", "https://x.test/#reset=abc", 60), s)
    url, kw = calls[0]
    assert url == "https://api.brevo.com/v3/smtp/email"
    assert kw["headers"]["api-key"] == "xkeysib-test"
    body = kw["json"]
    assert body["sender"] == {"name": "Payoff", "email": "me@gmail.com"}
    assert body["to"] == [{"email": "you@example.com"}]
    assert "https://x.test/#reset=abc" in body["textContent"] and "https://x.test/#reset=abc" in body["htmlContent"]

    s = _settings(EMAIL_BACKEND="brevo", BREVO_API_KEY="k", EMAIL_FROM="me@gmail.com", EMAIL_FROM_NAME=" ")
    mailer._brevo(mailer.setup_check("you@example.com"), s)
    assert calls[1][1]["json"]["sender"]["name"] == "Payoff"  # a blank name falls back


def test_brevo_errors(monkeypatch):
    class Denied:
        status_code, text = 401, '{"code":"unauthorized","message":"Key not found"}'

    monkeypatch.setattr(requests, "post", lambda url, **kw: Denied())
    s = _settings(EMAIL_BACKEND="brevo", BREVO_API_KEY="bad", EMAIL_FROM="me@gmail.com")
    with pytest.raises(mailer.EmailError, match="401"):
        mailer._brevo(mailer.setup_check("you@example.com"), s)
    with pytest.raises(mailer.EmailError, match="BREVO_API_KEY"):
        mailer._brevo(mailer.setup_check("you@example.com"), _settings(EMAIL_BACKEND="brevo"))


def test_html_is_escaped():
    msg = mailer.password_reset('<b>"x"</b>@example.com', 'https://x.test/#reset=a"b', 60)
    assert "<b>\"x\"</b>" not in msg.html and "&lt;b&gt;" in msg.html and 'a"b' not in msg.html


def test_config_problems():
    assert mailer.config_problems(_settings()) == []  # local defaults are fine
    assert any("console" in p for p in mailer.config_problems(_settings(COOKIE_SECURE="true")))
    assert any("BREVO_API_KEY" in p for p in mailer.config_problems(_settings(EMAIL_BACKEND="brevo")))
    assert any("isn't supported" in p for p in mailer.config_problems(_settings(EMAIL_BACKEND="smtp")))


def test_smtp_key_flagged():
    s = _settings(EMAIL_BACKEND="brevo", BREVO_API_KEY="xsmtpsib-abc", EMAIL_FROM="me@gmail.com")
    assert any("SMTP key" in p for p in mailer.config_problems(s))


def test_cli_test_email_on_console_says_nothing_sent(outbox):
    from argparse import Namespace

    from app import cli
    with pytest.raises(SystemExit) as exc:
        cli.send_test_email(Namespace(email="you@example.com"))
    assert "Nothing sent" in str(exc.value.code) and outbox == []


def test_app_url_host_is_allowed():
    s = _settings(APP_URL="https://qqq-payoff.onrender.com/", ALLOWED_HOSTS="")
    assert "qqq-payoff.onrender.com" in s.host_allowlist
