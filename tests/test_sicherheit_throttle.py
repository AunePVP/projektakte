"""SEC-05 (round 2): nobody behind a shared address (terminal server, NAT, Docker gateway) can lock
out a colleague with valid credentials. Failures of other user names only space attempts out
(soft brake); HTTP Basic clients (monitoring) are only rejected after failures for their own name
from their own address."""
import base64
import secrets

import anyio
import pytest
from fastapi.testclient import TestClient

import projektakte.auth as auth_mod
from projektakte.auth import (CLIENT, DEVICE_COOKIE, PAIR, SOFT_QUEUE_SECONDS, SOFT_SPACING_CAP, USER, Auth,
                              LoginThrottle, hash_password)
from projektakte.config import load_settings
from projektakte.main import create_app

PW = "Brandschutz-2025!"
MONITOR_PW = "monitoring-" + "x" * 20
USERS = {"anna": hash_password(PW, iterations=1000), "demo": hash_password(PW, iterations=1000),
         "monitor": hash_password(MONITOR_PW, iterations=1000)}
KEY = secrets.token_urlsafe(48)
SHARED = "10.0.0.5"  # terminal server: attacker and victims share the address


@pytest.fixture(autouse=True)
def _cheap_dummy_hash(monkeypatch):
    monkeypatch.setattr(auth_mod, "_DUMMY", [hash_password("dummy", iterations=1000)])


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_auth(clock: FakeClock) -> Auth:
    return Auth(dict(USERS), KEY, throttle=LoginThrottle(clock=clock), sleep=clock.sleep)


def attempt(auth, user, password, client, **kw):
    return anyio.run(lambda: auth.authenticate(user, password, client, **kw))


# -- unit level (fake clock) ------------------------------------------------------------------------------
def test_name_spraying_never_blocks_a_correct_password():
    clock = FakeClock()
    auth = make_auth(clock)
    for i in range(CLIENT.free + 30):  # random names from the shared address
        r = attempt(auth, f"zufall{i}", "falsch", SHARED)
        assert r.status == "invalid" and r.retry_after == 0  # nothing to wait for: soft brake only
    assert max(clock.slept) <= SOFT_SPACING_CAP
    for user in ("demo", "anna"):
        started = clock.t
        r = attempt(auth, user, PW, SHARED)  # new browser without device cookie
        assert r.ok and clock.t - started <= SOFT_SPACING_CAP
        r = attempt(auth, user, PW, SHARED, basic=True)  # script/monitoring with HTTP Basic
        assert r.ok


def test_slow_attacker_for_an_hour_never_throttles_the_victim():
    """The verifier's simulation (sim_shared_ip.py): an attacker who always retries as soon as he may,
    the victim (no device cookie, correct password) every 30 s from the same address."""
    clock = FakeClock()
    auth = make_auth(clock)
    end = clock.t + 3600
    attacker, victim_ok, victim_total, victim_waits = 0, 0, 0, []
    while clock.t < end:
        r = attempt(auth, f"zufall{attacker}", "falsch", SHARED)
        attacker += 1
        clock.advance(max(r.retry_after, 1))
        if attacker % 30 == 0:
            started = clock.t
            v = attempt(auth, "demo", PW, SHARED)
            victim_total += 1
            victim_ok += v.ok
            victim_waits.append(clock.t - started)
    assert attacker > 1000 and victim_total > 30
    assert victim_ok == victim_total  # never throttled …
    assert max(victim_waits) <= 2 * SOFT_SPACING_CAP  # … at most delayed a little


def test_soft_brake_spaces_out_attempts():
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock)
    keys = {"client:10.9.9.9": CLIENT}
    for _ in range(CLIENT.free + 5):
        throttle.failure(keys)
    waits = [throttle.reserve(keys, SOFT_QUEUE_SECONDS) for _ in range(5)]  # five parallel attempts
    assert waits == [0.0, CLIENT.cap, 2 * CLIENT.cap, 3 * CLIENT.cap, 4 * CLIENT.cap]
    assert throttle.retry_after(keys) == 0  # a soft brake never rejects
    assert throttle.reserve({"client:10.9.9.8": CLIENT}, SOFT_QUEUE_SECONDS) == 0  # other clients: no wait


def test_flood_from_one_address_is_turned_away_cheaply(monkeypatch):
    """Hundreds of parallel attempts from one address: the queue is bounded, the rest is answered
    "busy" without a password check, and other addresses are not affected."""
    clock = FakeClock()
    auth = make_auth(clock)
    for i in range(CLIENT.free + 4):
        attempt(auth, f"zufall{i}", "falsch", SHARED)

    async def no_sleep(seconds):  # all 200 attempts arrive at the same moment
        clock.slept.append(seconds)

    auth._sleep = no_sleep
    checked = []
    real_verify = auth._verify
    monkeypatch.setattr(auth, "_verify", lambda u, p: checked.append(u) or real_verify(u, p))
    statuses = []

    async def flood():
        async def one(i):
            statuses.append((await auth.authenticate(f"flut{i}", "falsch", SHARED)).status)

        async with anyio.create_task_group() as tg:
            for i in range(200):
                tg.start_soon(one, i)

    anyio.run(flood)
    assert statuses.count("busy") >= 150 and len(checked) <= SOFT_QUEUE_SECONDS / CLIENT.cap + 2
    assert attempt(auth, "demo", PW, "10.0.0.6").ok  # another workstation: unaffected
    clock.advance(SOFT_QUEUE_SECONDS + 1)
    assert attempt(auth, "demo", PW, SHARED).ok  # the flood is over: the colleague gets in again


def test_targeted_failures_still_throttle_that_name():
    """Guessing one user's password stays throttled hard (per address and user, per user)."""
    clock = FakeClock()
    auth = make_auth(clock)
    for _ in range(PAIR.free):
        attempt(auth, "anna", "geraten", SHARED)
    assert attempt(auth, "anna", PW, SHARED).status == "throttled"
    assert attempt(auth, "anna", PW, SHARED, basic=True).status == "throttled"  # same user, same address
    assert attempt(auth, "demo", PW, SHARED).ok  # colleagues behind the same address are not affected
    assert attempt(auth, "anna", PW, "10.0.0.6").ok  # anna from her own workstation neither


def test_basic_client_only_locked_out_per_user_and_address():
    clock = FakeClock()
    auth = make_auth(clock)
    monitoring_host = "10.0.0.50"
    for i in range(USER.free + 5):  # distributed guessing of the monitoring account's password
        clock.advance(auth.throttle.retry_after({"user:monitor": USER}))  # as soon as allowed
        assert attempt(auth, "monitor", "geraten", f"10.1.{i}.1").status == "invalid"
    for i in range(CLIENT.free + 5):  # and name spraying from the monitoring host's address
        attempt(auth, f"zufall{i}", "geraten", monitoring_host)
    # the login form applies the per-user brake hard (only a browser with device cookie is exempt) …
    assert attempt(auth, "monitor", MONITOR_PW, "10.0.0.51").status == "throttled"
    # … the Basic client is only delayed (at most a few seconds), never locked out
    started = clock.t
    assert attempt(auth, "monitor", MONITOR_PW, monitoring_host, basic=True).ok
    assert clock.t - started <= SOFT_SPACING_CAP
    # failures for the monitoring account from its own address do lock it out – that is per user+address
    for _ in range(PAIR.free):
        attempt(auth, "monitor", "geraten", monitoring_host, basic=True)
    assert attempt(auth, "monitor", MONITOR_PW, monitoring_host, basic=True).status == "throttled"


def test_basic_failures_still_count_for_the_login_form():
    """HTTP Basic is no way around the per-user brake of the form login."""
    clock = FakeClock()
    auth = make_auth(clock)
    for i in range(USER.free):
        attempt(auth, "anna", "geraten", f"10.2.{i}.1", basic=True)
    assert attempt(auth, "anna", PW, "10.0.0.7").status == "throttled"


def test_failure_message_shows_only_hard_waits():
    clock = FakeClock()
    auth = make_auth(clock)
    results = [attempt(auth, f"zufall{i}", "falsch", SHARED) for i in range(CLIENT.free + 3)]
    assert all(r.retry_after == 0 for r in results)  # "Nächster Versuch in …" would be wrong here


# -- end to end through the application (shared address "testclient") -------------------------------------
@pytest.fixture()
def client(tmp_path):
    users = ",".join(f"{name}:{encoded}" for name, encoded in USERS.items())
    s = load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test", "SECRET_KEY": KEY, "AUTH_USERS": users})
    app = create_app(s, background=False)
    clock = FakeClock()  # waiting behind the soft brake costs no real time
    app.state.auth.throttle = LoginThrottle(clock=clock)
    app.state.auth._sleep = clock.sleep
    return TestClient(app)


def form_login(client, user, password):
    return client.post("/login", data={"username": user, "password": password, "next": "/"},
                       follow_redirects=False)


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_shared_address_end_to_end(client):
    attacker = TestClient(client.app)
    for i in range(CLIENT.free):
        r = form_login(attacker, f"zufall{i}", "falsch")
        assert r.status_code == 401 and "Nächster Versuch" not in r.text
    for i in range(3):  # also via HTTP Basic
        assert attacker.get("/api/version", headers=basic(f"skript{i}", "falsch")).status_code == 401
    victim = TestClient(client.app)  # first login in a new browser – no device cookie
    r = form_login(victim, "demo", PW)
    assert r.status_code == 303 and r.cookies.get(DEVICE_COOKIE)
    assert TestClient(client.app).get("/health/deps", headers=basic("monitor", MONITOR_PW)).status_code == 200
    assert TestClient(client.app).get("/api/version", headers=basic("anna", PW)).status_code == 200


def test_busy_answer_for_a_flood(client, monkeypatch):
    """The address's queue is full (round 3: the flooder gets "busy", colleagues get in – see
    test_sicherheit_r3_flood.py)."""
    def reserve(keys, max_wait, now=None):
        return None if any(key.startswith("client:") for key in keys) else 0.0

    monkeypatch.setattr(client.app.state.auth.throttle, "reserve", reserve)
    r = form_login(client, "zufall", "falsch")
    assert r.status_code == 503 and r.headers["Retry-After"] == "5" and "sehr viele Anmeldungen" in r.text
    r = client.get("/api/version", headers=basic("skript", "falsch"))
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert form_login(TestClient(client.app), "demo", PW).status_code == 303
    assert client.get("/api/version", headers=basic("monitor", MONITOR_PW)).status_code == 200
