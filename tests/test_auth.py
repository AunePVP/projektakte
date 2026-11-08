"""Access control: without login no data; only /health (and login/static) are public."""
import base64
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from projektakte.auth import Auth, hash_password, parse_users, safe_next, verify_password
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app

PW = "Brandschutz-2025!"
HASH = hash_password(PW, iterations=1000)  # few iterations keep the tests fast


def settings(tmp_path, **extra):
    env = {"DATA_DIR": str(tmp_path), "DEMO_MODE": "1", "AUTH_USERS": f"anna:{HASH}",
           "SECRET_KEY": "test-geheimnis", "APP_ENV": "test"}
    env.update(extra)
    return load_settings(env)


@pytest.fixture()
def app_client(tmp_path):
    generate(tmp_path / "demo")
    s = settings(tmp_path)
    run_index(s)
    return TestClient(create_app(s, background=False))


def login(client, user="anna", password=PW, next_="/"):
    return client.post("/login", data={"username": user, "password": password, "next": next_},
                       follow_redirects=False)


def test_password_hashing():
    h = hash_password("geheim", iterations=1000)
    assert h.startswith("pbkdf2_sha256$1000$") and verify_password("geheim", h)
    assert not verify_password("falsch", h) and not verify_password("geheim", "kaputt")
    assert hash_password("geheim", iterations=1000) != h  # random salt


def test_parse_users(tmp_path):
    f = tmp_path / "users"
    f.write_text(f"# Kommentar\nben:{HASH}\n")
    users = parse_users(f"anna:{HASH}, kaputt:abc", str(f))
    assert set(users) == {"anna", "ben"}


@pytest.mark.parametrize("path", ["/", "/projekte/BS-2024-0137", "/eingang", "/fragen", "/status"])
def test_pages_redirect_to_login(app_client, path):
    r = app_client.get(path, headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=")


@pytest.mark.parametrize("method,path", [("GET", "/api/projects"), ("GET", "/api/mails"),
                                         ("GET", "/api/search?q=x"), ("POST", "/api/ask"),
                                         ("GET", "/api/inbox"), ("GET", "/health/deps"),
                                         ("POST", "/api/mails/1/assign"), ("GET", "/dokumente/1/datei")])
def test_api_needs_login(app_client, method, path):
    r = app_client.request(method, path, json={"question": "x"})
    assert r.status_code == 401 and r.json()["detail"] == "Anmeldung erforderlich"


def test_public_endpoints(app_client):
    assert app_client.get("/health").json()["status"] == "ok"
    assert app_client.get("/static/app.css").status_code == 200
    assert "Anmelden" in app_client.get("/login").text


def test_login_flow(app_client):
    r = login(app_client, password="falsch")
    assert r.status_code == 401 and "falsch" in r.text
    r = login(app_client, next_="/projekte/BS-2024-0137")
    assert r.status_code == 303 and r.headers["location"] == "/projekte/BS-2024-0137"
    cookie = r.cookies.get("projektakte_session") or app_client.cookies.get("projektakte_session")
    assert cookie
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    page = app_client.get("/")
    assert page.status_code == 200 and "Angemeldet: anna" in page.text
    assert app_client.get("/api/projects").json()["count"] == 8
    # decisions are recorded with the user name
    mid = app_client.get("/api/inbox").json()["mails"][0]["id"]
    r = app_client.post(f"/api/mails/{mid}/assign", json={"action": "none"}).json()
    assert "anna" in r["assign_reason"]
    # logout
    app_client.post("/logout")
    assert app_client.get("/api/projects").status_code == 401


def test_open_redirect_blocked(app_client):
    assert login(app_client, next_="//evil.example/x").headers["location"] == "/"
    assert safe_next("https://evil.example") == "/" and safe_next("/eingang") == "/eingang"


def test_tampered_and_expired_cookies(tmp_path):
    s = settings(tmp_path)
    auth = Auth.from_settings(s)
    good = auth.make_cookie("anna")
    assert auth.read_cookie(good) == "anna"
    payload, sig = good.rsplit(".", 1)
    forged = base64.urlsafe_b64encode(b'{"u":"anna","exp":9999999999}').rstrip(b"=").decode() + "." + sig
    assert auth.read_cookie(forged) is None
    assert auth.read_cookie(good[:-2] + "xx") is None
    other_key = Auth({"anna": HASH}, "anderer-schluessel")
    assert other_key.read_cookie(good) is None
    auth.session_seconds = -1
    assert auth.read_cookie(auth.make_cookie("anna")) is None
    removed = Auth({}, "test-geheimnis")  # user removed from the configuration
    assert removed.read_cookie(good) is None


def test_basic_auth_for_api(app_client):
    token = base64.b64encode(f"anna:{PW}".encode()).decode()
    r = app_client.get("/api/projects", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 200
    bad = base64.b64encode(b"anna:falsch").decode()
    assert app_client.get("/api/projects", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    # Basic credentials are not accepted for browser pages (cookie login only)
    assert app_client.get("/", headers={"Authorization": f"Basic {token}"},
                          follow_redirects=False).status_code == 401


def test_lockout_after_failures(tmp_path):
    auth = Auth({"anna": HASH}, "k")
    for _ in range(5):
        assert not auth.check_credentials("anna", "falsch", "1.2.3.4")
    assert not auth.check_credentials("anna", PW, "1.2.3.4")  # locked
    assert auth.check_credentials("anna", PW, "5.6.7.8")


def test_htmx_requests_get_redirect_header(app_client):
    r = app_client.post("/fragen", data={"question": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


def test_auth_disabled_not_in_production(tmp_path):
    assert Auth.from_settings(settings(tmp_path, AUTH_DISABLED="1")).disabled is True
    assert Auth.from_settings(settings(tmp_path, AUTH_DISABLED="1", APP_ENV="production")).disabled is False


def test_no_users_configured(tmp_path):
    client = TestClient(create_app(settings(tmp_path, AUTH_USERS=""), background=False))
    assert "keine Benutzer eingerichtet" in client.get("/login").text
    assert client.get("/api/projects").status_code == 401


def test_passwd_cli():
    out = subprocess.run([sys.executable, "-m", "projektakte.passwd", "lea", "--stdin"],
                         input="ein-langes-passwort\n", capture_output=True, text=True, check=True).stdout
    name, encoded = out.strip().split(":", 1)
    assert name == "lea" and verify_password("ein-langes-passwort", encoded)


def test_secrets_never_logged(tmp_path, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    s = settings(tmp_path, SECRET_KEY="", PLMAIL_TOKEN="plmail_" + "a" * 64)
    Auth.from_settings(s)
    assert "test-geheimnis" not in caplog.text and HASH not in caplog.text and "plmail_aaaa" not in caplog.text
