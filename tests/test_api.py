from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import HDR, signup

LEG = {"id": "l1", "include": True, "side": -1, "qty": 1, "type": "P", "strike": 590, "exp": "2026-11-20",
       "entry": 4.25, "symbol": "QQQ261120P00590000", "lastIv": 0.2, "openedAt": "2026-11-07T15:00:00Z",
       "exitPrice": None, "closedAt": None}
BODY = {"name": "Short put", "symbol": "qqq", "legs": [LEG],
        "scenario": {"ivShift": 2, "targetMin": 60, "spotPct": 0, "r": 0.04, "q": 0.006}, "notes": "test"}


# ---- pages and market data -----------------------------------------------------------------
def test_index_and_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and "<title>Payoff</title>" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_market_endpoints(client):
    assert client.get("/api/expirations?symbol=QQQ").json()["expirations"] == ["2026-11-20"]
    assert client.get("/api/chain?symbol=QQQ&expiration=2026-11-20").json()["spot"] == 600.0
    assert client.get("/api/chain?symbol=QQQ&expiration=bad").status_code == 400
    assert client.get("/api/expirations?symbol=1abc").status_code == 400


def test_unknown_host_rejected(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/expirations", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/healthz", headers={"Host": "10.0.0.5:10000"}).status_code == 200


# ---- auth ----------------------------------------------------------------------------------
def test_signup_login_logout(client):
    signup(client, "Trader@Example.com")
    assert client.get("/api/auth/me").json() == {"email": "trader@example.com"}
    assert client.post("/api/auth/logout", headers=HDR).status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    r = client.post("/api/auth/login", json={"email": "trader@example.com", "password": "correct horse battery"},
                    headers=HDR)
    assert r.status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_cookie_flags(client):
    cookie = signup(client).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_signup_rules(client):
    r = client.post("/api/auth/signup", json={"email": "b@example.com", "password": "short"}, headers=HDR)
    assert r.status_code == 422
    signup(client, "b@example.com")
    client.cookies.clear()
    r = client.post("/api/auth/signup", json={"email": "B@example.com", "password": "another long one"},
                    headers=HDR)
    assert r.status_code == 409


def test_wrong_password_and_lockout(client):
    signup(client)
    client.cookies.clear()
    bad = {"email": "a@example.com", "password": "wrong password!"}
    for _ in range(8):
        assert client.post("/api/auth/login", json=bad, headers=HDR).status_code == 401
    good = {"email": "a@example.com", "password": "correct horse battery"}
    assert client.post("/api/auth/login", json=good, headers=HDR).status_code == 429


def test_bad_email_message(client):
    r = client.post("/api/auth/signup", json={"email": "not-an-email", "password": "long enough pw"}, headers=HDR)
    assert r.status_code == 422 and r.json()["error"] == "Enter a valid email address."


def test_unknown_email_same_error(client):
    r = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "whatever123"}, headers=HDR)
    assert r.status_code == 401 and r.json()["error"] == "Email or password is incorrect."


def test_password_hash_not_plaintext(client):
    from sqlalchemy import text
    from app.db import get_engine
    signup(client)
    with get_engine().connect() as conn:
        h = conn.execute(text("select password_hash from users")).scalar_one()
    assert h.startswith("$argon2") and "correct horse" not in h


def test_csrf_header_required(client):
    r = client.post("/api/auth/signup", json={"email": "c@example.com", "password": "long enough pw"})
    assert r.status_code == 403
    r = client.post("/api/auth/signup", json={"email": "c@example.com", "password": "long enough pw"},
                    headers={**HDR, "Origin": "https://evil.example"})
    assert r.status_code == 403


# ---- strategies ----------------------------------------------------------------------------
def test_strategies_require_login(client):
    assert client.get("/api/strategies").status_code == 401
    assert client.post("/api/strategies", json=BODY, headers=HDR).status_code == 401


def test_strategy_crud(client):
    signup(client)
    r = client.post("/api/strategies", json=BODY, headers=HDR)
    assert r.status_code == 201
    s = r.json()
    assert s["symbol"] == "QQQ" and s["legs"][0]["strike"] == 590 and s["scenario"]["ivShift"] == 2

    listing = client.get("/api/strategies").json()
    assert [x["name"] for x in listing] == ["Short put"] and listing[0]["open_legs"] == 1

    r = client.patch(f"/api/strategies/{s['id']}", json={"name": "Renamed", "notes": "n2"}, headers=HDR)
    assert r.json()["name"] == "Renamed" and r.json()["legs"] == s["legs"]
    assert r.json()["updated_at"] >= s["updated_at"]

    assert client.delete(f"/api/strategies/{s['id']}", headers=HDR).status_code == 204
    assert client.get(f"/api/strategies/{s['id']}").status_code == 404


def test_strategy_validation(client):
    signup(client)
    bad_leg = {**LEG, "side": 2}
    assert client.post("/api/strategies", json={**BODY, "legs": [bad_leg]}, headers=HDR).status_code == 422
    assert client.post("/api/strategies", json={**BODY, "name": "   "}, headers=HDR).status_code == 422
    assert client.post("/api/strategies", json={**BODY, "symbol": "<script>"}, headers=HDR).status_code == 422
    assert client.post("/api/strategies", json={**BODY, "legs": [LEG] * 51}, headers=HDR).status_code == 422


def test_users_cannot_see_each_others_strategies(client):
    signup(client, "alice@example.com")
    sid = client.post("/api/strategies", json=BODY, headers=HDR).json()["id"]

    with TestClient(create_app(connect_source=False)) as bob:
        signup(bob, "bob@example.com")
        assert bob.get("/api/strategies").json() == []
        assert bob.get(f"/api/strategies/{sid}").status_code == 404
        assert bob.patch(f"/api/strategies/{sid}", json={"name": "x"}, headers=HDR).status_code == 404
        assert bob.delete(f"/api/strategies/{sid}", headers=HDR).status_code == 404

    assert client.get(f"/api/strategies/{sid}").json()["name"] == "Short put"
