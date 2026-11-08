"""Access control: without login no data; only /health, /health/ready (and login/static) are public."""
import base64
import logging
import secrets
import subprocess
import sys
import time

import anyio
import pytest
from fastapi.testclient import TestClient

import projektakte.auth as auth_mod
from projektakte.auth import (CLIENT, COOKIE, DEVICE_COOKIE, PAIR, Auth, LoginThrottle, SessionEpochs, client_key,
                              hash_password, parse_basic, parse_users, safe_next, verify_password)
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.startup import ConfigError

PW = "Brandschutz-2025!"
HASH = hash_password(PW, iterations=1000)  # few iterations keep the tests fast
STRONG_KEY = secrets.token_urlsafe(48)


@pytest.fixture(autouse=True)
def _cheap_dummy_hash(monkeypatch):
    """Unknown user names are verified against a dummy hash – make it cheap for the tests."""
    monkeypatch.setattr(auth_mod, "_DUMMY", [hash_password("dummy", iterations=1000)])


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


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


@pytest.fixture()
def light_client(tmp_path):
    """App without demo data – for tests of the login mechanics only."""
    return TestClient(create_app(settings(tmp_path, DEMO_MODE="0"), background=False))


def login(client, user="anna", password=PW, next_="/", **kw):
    return client.post("/login", data={"username": user, "password": password, "next": next_},
                       follow_redirects=False, **kw)


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
    assert app_client.get("/health/ready").json()["checks"]["database"] == "ok"
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


def test_open_redirect_blocked(light_client):
    assert login(light_client, next_="//evil.example/x").headers["location"] == "/"
    assert safe_next("https://evil.example") == "/" and safe_next("/eingang") == "/eingang"


def test_tampered_and_expired_cookies(tmp_path):
    s = settings(tmp_path)
    auth = Auth.from_settings(s)
    good = auth.make_cookie("anna")
    assert auth.read_cookie(good) == "anna"
    payload, sig = good.rsplit(".", 1)
    forged = base64.urlsafe_b64encode(b'{"v":2,"u":"anna","iat":1,"exp":9999999999,"e":0}').rstrip(b"=").decode()
    assert auth.read_cookie(forged + "." + sig) is None
    assert auth.read_cookie(good[:-2] + "xx") is None
    other_key = Auth({"anna": HASH}, "anderer-schluessel")
    assert other_key.read_cookie(good) is None
    auth.session_seconds = -1
    assert auth.read_cookie(auth.make_cookie("anna")) is None
    removed = Auth({}, "test-geheimnis")  # user removed from the configuration
    assert removed.read_cookie(good) is None


# -- BUG-10 / SEC-03: malformed cookies are "no session", never a server error --------------------------
@pytest.mark.parametrize("raw", [b"abc.\xc3\xa4", b"\xc3\xa4bc.def", b"\xff\xfe.\x80", b"a" * 5000 + b".b",
                                 b"....", b"x.y", b".", b"%%%.***"])
def test_malformed_cookie_never_causes_500(light_client, raw):
    headers = {"Cookie": b"projektakte_session=" + raw}
    assert light_client.get("/health", headers=headers).status_code == 200
    assert light_client.get("/health/ready", headers=headers).status_code == 200
    assert light_client.get("/login", headers=headers).status_code == 200
    assert light_client.get("/api/status", headers=headers).status_code == 401


@pytest.mark.parametrize("value", ["ä.ä", "abc.ä", "äbc.def", "a" * 5000, "eyJ2Ijoy.sig", "e30.sig",
                                   None, "", "nopoint"])
def test_read_cookie_rejects_garbage(value):
    auth = Auth({"anna": HASH}, STRONG_KEY)
    assert auth.read_cookie(value) is None


def test_read_cookie_rejects_wrong_types():
    auth = Auth({"anna": HASH}, STRONG_KEY)
    now = int(time.time())
    for data in ([1, 2], {"v": 2, "u": "anna", "iat": "1", "exp": now + 60, "e": 0},
                 {"v": 2, "u": ["anna"], "iat": now, "exp": now + 60, "e": 0},
                 {"v": 2, "u": "anna", "iat": now, "exp": now + 60, "e": True},
                 {"u": "anna", "exp": now + 60}):  # old cookie format without iat/epoch
        assert auth.read_cookie(auth._seal(auth._session_key, data)) is None


# -- DEMO-6 / SEC-06: logout ends the session server-side ------------------------------------------------
def test_logout_invalidates_copied_cookie(light_client):
    r = login(light_client)
    copied = r.cookies.get(COOKIE)
    other_device = TestClient(light_client.app)
    other_device.cookies.set(COOKIE, login(other_device).cookies.get(COOKIE))
    assert other_device.get("/api/version").status_code == 200
    light_client.post("/logout")
    thief = TestClient(light_client.app)
    thief.cookies.set(COOKIE, copied)
    assert thief.get("/api/version").status_code == 401
    assert other_device.get("/api/version").status_code == 401  # all sessions of the user end
    page = light_client.get("/login?abgemeldet=1")
    assert "Alle Sitzungen" in page.text
    login(light_client)
    assert light_client.get("/api/version").status_code == 200  # a new login works


def test_logout_survives_restart(tmp_path):
    s = settings(tmp_path, DEMO_MODE="0")
    first = TestClient(create_app(s, background=False))
    copied = login(first).cookies.get(COOKIE)
    first.post("/logout")
    restarted = TestClient(create_app(s, background=False))
    restarted.cookies.set(COOKIE, copied)
    assert restarted.get("/api/version").status_code == 401


def test_session_epoch_in_meta_table(tmp_path):
    epochs = SessionEpochs(tmp_path / "x.sqlite3")
    assert epochs.current("anna") == 0
    assert epochs.bump("anna") == 1 and epochs.bump("anna") == 2
    assert SessionEpochs(tmp_path / "x.sqlite3").current("anna") == 2  # persisted
    assert epochs.current("ben") == 0


def test_cookie_lifetime_bounded_server_side():
    clock = FakeClock(2_000_000_000)
    auth = Auth({"anna": HASH}, STRONG_KEY, session_hours=1, clock=clock)
    now = int(clock())
    long_lived = auth._seal(auth._session_key, {"v": 2, "u": "anna", "iat": now, "exp": now + 10 * 365 * 86400,
                                                "e": 0})
    assert auth.read_cookie(long_lived) == "anna"
    clock.advance(3601)
    assert auth.read_cookie(long_lived) is None  # iat + SESSION_HOURS wins over a longer exp
    future = auth._seal(auth._session_key, {"v": 2, "u": "anna", "iat": int(clock()) + 3600,
                                            "exp": int(clock()) + 7200, "e": 0})
    assert auth.read_cookie(future) is None


def test_session_hours_bounded():
    assert auth_mod.bounded_session_hours(10_000) == auth_mod.MAX_SESSION_HOURS
    assert auth_mod.bounded_session_hours(0) == 12.0
    assert auth_mod.bounded_session_hours(8) == 8


@pytest.mark.parametrize("key", ["", "x", "test-geheimnis", "a" * 64, "ab" * 32])
def test_weak_secret_key_refused_in_production(tmp_path, key):
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        Auth.from_settings(settings(tmp_path, APP_ENV="production", SECRET_KEY=key))


def test_weak_secret_key_warns_elsewhere(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    Auth.from_settings(settings(tmp_path, APP_ENV="staging", SECRET_KEY="kurz"))
    assert "kürzer als 32 Zeichen" in caplog.text and "kurz" not in caplog.text.replace("kürzer", "")
    assert Auth.from_settings(settings(tmp_path, APP_ENV="production", SECRET_KEY=STRONG_KEY)).users


# -- SEC-07: AUTH_DISABLED only in dev -----------------------------------------------------------------
@pytest.mark.parametrize("env", ["production", "Production", "prod", "staging", "STAGING", "demo", "live"])
def test_auth_disabled_refused_outside_dev(tmp_path, env):
    with pytest.raises(ConfigError, match="APP_ENV=dev"):
        Auth.from_settings(settings(tmp_path, AUTH_DISABLED="1", APP_ENV=env))
    with pytest.raises(ConfigError):
        create_app(settings(tmp_path, AUTH_DISABLED="1", APP_ENV=env), background=False)


@pytest.mark.parametrize("env", ["dev", "DEV", "test"])
def test_auth_disabled_allowed_in_dev(tmp_path, env):
    app = create_app(settings(tmp_path, AUTH_DISABLED="1", APP_ENV=env, DEMO_MODE="0"), background=False)
    assert app.state.auth.disabled is True
    assert TestClient(app).get("/api/version").status_code == 200


def test_auth_disabled_default_env_is_dev(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "AUTH_DISABLED": "1"})
    assert Auth.from_settings(s).disabled is True


# -- SEC-01: secure cookies in production ----------------------------------------------------------------
def test_cookie_secure_defaults_on_in_production(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    prod = dict(APP_ENV="production", SECRET_KEY=STRONG_KEY)
    assert Auth.from_settings(settings(tmp_path, **prod)).cookie_secure is True
    assert Auth.from_settings(settings(tmp_path)).cookie_secure is False  # dev/test: off by default
    assert Auth.from_settings(settings(tmp_path, COOKIE_SECURE="1")).cookie_secure is True
    monkeypatch.setenv("COOKIE_SECURE", "0")  # explicit override, with a warning
    caplog.set_level(logging.WARNING)
    assert Auth.from_settings(settings(tmp_path, COOKIE_SECURE="0", **prod)).cookie_secure is False
    assert "COOKIE_SECURE=0" in caplog.text


def test_production_login_sets_secure_cookie(tmp_path, monkeypatch):
    monkeypatch.delenv("COOKIE_SECURE", raising=False)
    app = create_app(settings(tmp_path, APP_ENV="production", SECRET_KEY=STRONG_KEY, DEMO_MODE="0"),
                     background=False)
    http = TestClient(app, base_url="http://projektakte.firma.de")
    assert "nur über HTTPS" in http.get("/login").text  # plain HTTP: explain why the login cannot work
    https = TestClient(app, base_url="https://projektakte.firma.de")
    assert "nur über HTTPS" not in https.get("/login").text
    r = login(https)
    assert r.status_code == 303
    cookies = r.headers.get_list("set-cookie")
    assert all("secure" in c.lower() for c in cookies)


# -- SEC-04: per-client limit, bounded table, limited PBKDF2 concurrency --------------------------------
def test_throttle_per_client_independent_of_user_name():
    clock = FakeClock()
    auth = Auth({"anna": HASH}, STRONG_KEY, throttle=LoginThrottle(clock=clock))
    for i in range(CLIENT.free):
        assert not auth.check_credentials(f"name{i}", "x", "10.0.0.9")  # a new name each time
    assert not auth.check_credentials("anna", PW, "10.0.0.9")  # the client is throttled now
    assert auth.check_credentials("anna", PW, "10.0.0.10")  # other clients are not
    clock.advance(CLIENT.cap + 1)
    assert auth.check_credentials("anna", PW, "10.0.0.9")  # and the wait ends


def test_failure_table_is_bounded_and_forgets():
    clock = FakeClock()
    throttle = LoginThrottle(max_entries=50, forget_after=900, clock=clock)
    for i in range(1000):
        throttle.failure({f"user:n{i}": PAIR})
    assert len(throttle) == 50
    clock.advance(901)
    throttle.failure({"user:new": PAIR})
    assert len(throttle) == 1  # stale entries are cleaned up


def test_ipv6_clients_grouped_by_network():
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::2") == "2001:db8:1:2::/64"
    assert client_key("::ffff:10.0.0.1") == "10.0.0.1" and client_key("10.0.0.1") == "10.0.0.1"
    assert client_key("") == "-" and client_key("testclient") == "testclient"


def test_parallel_password_checks_are_limited(monkeypatch):
    auth = Auth({"anna": HASH}, STRONG_KEY)
    auth._slots = anyio.Semaphore(2)
    running, peak = [0], [0]

    def slow_verify(user, password):
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        time.sleep(0.02)
        running[0] -= 1
        return False

    monkeypatch.setattr(auth, "_verify", slow_verify)

    async def burst():
        async with anyio.create_task_group() as tg:
            for i in range(12):
                tg.start_soon(auth.authenticate, f"n{i}", "x", f"10.1.0.{i}")

    anyio.run(burst)
    assert peak[0] == 2


def test_queued_burst_is_rechecked_after_waiting(monkeypatch):
    """300 parallel attempts from one client do not cause 300 PBKDF2 runs: queued attempts are
    checked against the throttle again once they get a slot."""
    auth = Auth({"anna": HASH}, STRONG_KEY)
    auth._slots = anyio.Semaphore(1)
    calls = []
    monkeypatch.setattr(auth, "_verify", lambda user, password: calls.append(user) or False)

    async def burst():
        results = []

        async def one():
            results.append(await auth.authenticate("anna", "falsch", "10.2.0.1"))

        async with anyio.create_task_group() as tg:
            for _ in range(300):
                tg.start_soon(one)
        return results

    results = anyio.run(burst)
    assert len(calls) == PAIR.free
    assert sum(r.status == "throttled" for r in results) == 300 - PAIR.free


def test_busy_when_no_slot_frees_up(monkeypatch):
    auth = Auth({"anna": HASH}, STRONG_KEY)
    auth._slots = anyio.Semaphore(1)
    monkeypatch.setattr(auth_mod, "VERIFY_QUEUE_SECONDS", 0.05)
    monkeypatch.setattr(auth, "_verify", lambda user, password: time.sleep(0.3) or False)

    async def two():
        results = []

        async def one(i):
            results.append(await auth.authenticate(f"n{i}", "x", f"10.3.0.{i}"))

        async with anyio.create_task_group() as tg:
            tg.start_soon(one, 1)
            tg.start_soon(one, 2)
        return sorted(r.status for r in results)

    assert anyio.run(two) == ["busy", "invalid"]


# -- SEC-05: back-off instead of a lock-out; nobody can lock out a colleague ----------------------------
def test_backoff_instead_of_permanent_lockout():
    clock = FakeClock()
    auth = Auth({"anna": HASH}, STRONG_KEY, throttle=LoginThrottle(clock=clock))
    shared_ip = "10.0.0.1"  # terminal server / proxy: attacker and anna share the address
    for _ in range(200):  # a persistent attacker tries again as soon as he may
        wait = auth.throttle.retry_after(auth._throttle_keys("anna", shared_ip, None))
        assert wait <= PAIR.cap  # the wait is bounded …
        clock.advance(wait)
        assert not auth.check_credentials("anna", "geraten", shared_ip)
    clock.advance(PAIR.cap)
    assert auth.check_credentials("anna", PW, shared_ip)  # … and always ends


def test_known_device_is_not_locked_out_by_others(light_client):
    r = login(light_client)
    device = r.cookies.get(DEVICE_COOKIE)
    assert device and "path=/login" in r.headers.get_list("set-cookie")[-1].lower()
    attacker = TestClient(light_client.app)  # same client address ("testclient")
    for _ in range(PAIR.free + 3):
        login(attacker, password="geraten")
    assert login(attacker).status_code == 429  # the shared address is throttled for anna …
    victim = TestClient(light_client.app)
    victim.cookies.set(DEVICE_COOKIE, device, path="/login")
    assert login(victim).status_code == 303  # … but not anna's own browser


def test_device_cookie_signed_and_bound_to_user():
    auth = Auth({"anna": HASH, "ben": HASH}, STRONG_KEY)
    cookie = auth.make_device_cookie("anna")
    assert auth.device_id(cookie, "anna") and auth.device_id(cookie, "Anna")
    assert auth.device_id(cookie, "ben") is None
    assert auth.device_id(cookie[:-3] + "abc", "anna") is None
    assert auth.read_cookie(cookie) is None  # a device cookie is no session
    assert Auth({"anna": HASH}, "ein-ganz-anderer-schluessel-1234567890").device_id(cookie, "anna") is None


def test_login_page_explains_throttling(light_client):
    for _ in range(PAIR.free - 1):
        assert login(light_client, password="falsch").status_code == 401
    r = login(light_client, password="falsch")
    assert r.status_code == 401 and "Nächster Versuch in 2 Sekunden" in r.text
    r = login(light_client)  # correct password, but within the wait
    assert r.status_code == 429 and "Zu viele Fehlversuche" in r.text and int(r.headers["Retry-After"]) >= 1


def test_basic_auth_for_api(app_client):
    token = base64.b64encode(f"anna:{PW}".encode()).decode()
    r = app_client.get("/api/projects", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 200
    bad = base64.b64encode(b"anna:falsch").decode()
    assert app_client.get("/api/projects", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    # Basic credentials are not accepted for browser pages (cookie login only)
    assert app_client.get("/", headers={"Authorization": f"Basic {token}"},
                          follow_redirects=False).status_code == 401


def test_basic_auth_throttled(light_client):
    bad = {"Authorization": "Basic " + base64.b64encode(b"anna:falsch").decode()}
    statuses = [light_client.get("/api/version", headers=bad).status_code for _ in range(PAIR.free + 1)]
    assert statuses[:PAIR.free] == [401] * PAIR.free and statuses[-1] == 429
    good = {"Authorization": "Basic " + base64.b64encode(f"anna:{PW}".encode()).decode()}
    r = light_client.get("/api/version", headers=good)
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_parse_basic():
    assert parse_basic("Basic " + base64.b64encode("anna:pä:ss".encode()).decode()) == ("anna", "pä:ss")
    for header in (None, "", "Bearer x", "Basic ä", "Basic !!!", "Basic " + base64.b64encode(b"ohne").decode(),
                   "Basic " + base64.b64encode(b"\xff:\xfe").decode()):
        assert parse_basic(header) is None


def test_lockout_after_failures():
    auth = Auth({"anna": HASH}, "k")
    for _ in range(5):
        assert not auth.check_credentials("anna", "falsch", "1.2.3.4")
    assert not auth.check_credentials("anna", PW, "1.2.3.4")  # throttled
    assert auth.check_credentials("anna", PW, "5.6.7.8")


def test_htmx_requests_get_redirect_header(app_client):
    r = app_client.post("/fragen", data={"question": "x"}, headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"


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
    caplog.set_level(logging.DEBUG)
    s = settings(tmp_path, SECRET_KEY="", PLMAIL_TOKEN="plmail_" + "a" * 64)
    Auth.from_settings(s)
    weak = settings(tmp_path, SECRET_KEY="schwach-aber-geheim", APP_ENV="staging")
    Auth.from_settings(weak)
    create_app(settings(tmp_path, SECRET_KEY=STRONG_KEY, DEMO_MODE="0", LLM_URL="http://nutzer:pw123@llm:8000/v1",
                        PLMAIL_URL="https://plmail.lan", PLMAIL_TOKEN="plmail_" + "b" * 64), background=False)
    for secret in ("test-geheimnis", HASH, "plmail_aaaa", "plmail_bbbb", "schwach-aber-geheim", STRONG_KEY, "pw123"):
        assert secret not in caplog.text
    assert "http://llm:8000/v1" in caplog.text  # the URL is logged without credentials


def test_unknown_user_names_not_logged(caplog):
    caplog.set_level(logging.WARNING)
    auth = Auth({"anna": HASH}, STRONG_KEY)
    for _ in range(PAIR.free):
        auth.check_credentials("MeinPasswort123", "x", "10.9.0.1")  # password typed into the name field
    for _ in range(PAIR.free):
        auth.check_credentials("anna", "x", "10.9.0.2")
    assert "MeinPasswort123" not in caplog.text and "meinpasswort123" not in caplog.text
    assert "Benutzer anna von 10.9.0.2" in caplog.text


def test_logout_reports_server_side_failure(light_client, monkeypatch):
    import sqlite3

    login(light_client)
    auth = light_client.app.state.auth

    def broken(user):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(auth.epochs, "bump", broken)
    r = light_client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?abgemeldet=teilweise"
    assert "erneut abmelden" in light_client.get(r.headers["location"]).text
