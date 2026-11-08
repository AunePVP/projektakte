"""verify3 SEC-05 / CLIENT brake (round 3): a flood from one address (terminal server, NAT, Docker
gateway) fills that address's queue – colleagues with a correct password and no device cookie and
HTTP Basic monitoring must still get in at once, without 503 or a 20 s wait. The flooder keeps
getting "busy", and the priority lane must neither reveal which user names exist nor allow
unthrottled guessing."""
import base64
import functools
import secrets
import time

import anyio
import pytest
from fastapi.testclient import TestClient

import projektakte.auth as auth_mod
from projektakte.auth import (CLIENT, DEVICE_COOKIE, PAIR, PRIORITY_CLAIMS, SOFT_QUEUE_SECONDS, SOFT_SPACING_CAP, Auth,
                              LoginThrottle, hash_password)
from projektakte.config import load_settings
from projektakte.main import create_app

PW = "Brandschutz-2025!"
MONITOR_PW = "monitoring-" + "x" * 20
USERS = {"anna": hash_password(PW, iterations=1000), "demo": hash_password(PW, iterations=1000),
         "monitor": hash_password(MONITOR_PW, iterations=1000)}
KEY = secrets.token_urlsafe(48)
SHARED = "10.0.0.5"
CHECK = 0.3  # simulated duration of one PBKDF2 check (seconds on the fake clock)


@pytest.fixture(autouse=True)
def _cheap_dummy_hash(monkeypatch):
    monkeypatch.setattr(auth_mod, "_DUMMY", [hash_password("dummy", iterations=1000)])


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def make_auth(clock: FakeClock, *, timed: bool = False) -> tuple[Auth, list[str]]:
    """Auth on a fake clock; ``checked`` lists every password check. ``timed``: a check takes CHECK
    seconds on the fake clock (to compare response times)."""
    auth = Auth(dict(USERS), KEY, throttle=LoginThrottle(clock=clock), sleep=clock.sleep)
    checked: list[str] = []
    real_verify = auth._verify

    def verify(user, password):
        checked.append(user)
        result = real_verify(user, password)
        if timed:
            clock.advance(CHECK)
            auth._check_seconds = CHECK  # the real (fast) hash must not change the simulated estimate
        return result

    auth._verify = verify
    if timed:
        auth._check_seconds = CHECK
    return auth, checked


def attempt(auth, user, password, client=SHARED, **kw):
    return anyio.run(lambda: auth.authenticate(user, password, client, **kw))


def fill_queue(auth, client=SHARED) -> None:
    """The state during a flood: the address has failed often and its queue is full."""
    for i in range(CLIENT.free + 5):
        auth.throttle.failure({f"client:{client}": CLIENT, f"pair:{client}|flut{i}": PAIR})
    while auth.throttle.reserve({f"client:{client}": CLIENT}, SOFT_QUEUE_SECONDS) is not None:
        pass


# -- the victims get in at once ---------------------------------------------------------------------------
def test_colleagues_get_in_at_once_while_the_queue_is_full():
    clock = FakeClock()
    auth, checked = make_auth(clock)
    fill_queue(auth)
    assert attempt(auth, "zufall1", "falsch").status == "busy"  # the flood itself is turned away …
    started = clock.t
    assert attempt(auth, "demo", PW).ok  # … a new browser without device cookie is not
    assert attempt(auth, "monitor", MONITOR_PW, basic=True).ok  # nor monitoring with HTTP Basic
    assert attempt(auth, "anna", PW, basic=True).ok
    assert clock.t - started < 0.1  # no waiting in the address's queue
    assert attempt(auth, "zufall2", "falsch").status == "busy"  # the queue is still full for the flooder
    assert checked.count("zufall1") == checked.count("zufall2") == 0  # no PBKDF2 work for the flood


def test_monitoring_keeps_working_through_a_long_flood():
    """Every successful login renews the priority: monitoring every 30 s for an hour of flooding."""
    clock = FakeClock()
    auth, _ = make_auth(clock)
    for _ in range(120):
        fill_queue(auth)
        assert attempt(auth, "monitor", MONITOR_PW, basic=True).ok
        assert attempt(auth, "demo", PW).ok
        clock.advance(30)


def test_flood_with_parallel_attempts_and_victims():
    """200 parallel flood attempts arriving at the same moment, the victims among them."""
    clock = FakeClock()
    auth, checked = make_auth(clock)
    for i in range(CLIENT.free + 4):
        attempt(auth, f"zufall{i}", "falsch")

    async def no_sleep(seconds):  # all attempts arrive at the same moment
        pass

    auth._sleep = no_sleep
    results: dict[str, list[str]] = {"flood": [], "demo": [], "monitor": []}

    async def flood():
        async def one(kind, user, password, **kw):
            results[kind].append((await auth.authenticate(user, password, SHARED, **kw)).status)

        async with anyio.create_task_group() as tg:
            for i in range(200):
                tg.start_soon(one, "flood", f"flut{i}", "falsch")
                if i in (50, 150):
                    tg.start_soon(one, "demo", "demo", PW)
                    tg.start_soon(functools.partial(one, "monitor", "monitor", MONITOR_PW, basic=True))

    anyio.run(flood)
    assert results["demo"] == ["ok", "ok"] and results["monitor"] == ["ok", "ok"]
    assert results["flood"].count("busy") >= 150  # overload protection for the flooder stays
    assert len([u for u in checked if u.startswith("flut")]) <= SOFT_QUEUE_SECONDS / SOFT_SPACING_CAP + 2


def test_flood_in_real_time(monkeypatch):
    """Real event loop, real clock (spacing and queue scaled down): 40 flooding connections from the
    shared address for one second; the victims' logins are not delayed by the queue."""
    monkeypatch.setattr(auth_mod, "SOFT_SPACING_CAP", 0.05)
    monkeypatch.setattr(auth_mod, "SOFT_QUEUE_SECONDS", 0.5)
    auth = Auth(dict(USERS), KEY)
    for i in range(CLIENT.free + 4):
        auth.throttle.failure({f"client:{SHARED}": CLIENT})
    flood_results: list[str] = []
    victims: list[tuple[str, float]] = []

    async def scenario():
        stop = time.monotonic() + 1.0

        async def flooder(k):
            i = 0
            while time.monotonic() < stop:
                flood_results.append((await auth.authenticate(f"f{k}x{i}", "falsch", SHARED)).status)
                i += 1

        async def victim(user, password, **kw):
            await anyio.sleep(0.3)  # the flood is in full swing
            for _ in range(4):
                started = time.monotonic()
                result = await auth.authenticate(user, password, SHARED, **kw)
                victims.append((result.status, time.monotonic() - started))
                await anyio.sleep(0.1)

        async with anyio.create_task_group() as tg:
            for k in range(40):
                tg.start_soon(flooder, k)
            tg.start_soon(victim, "demo", PW)
            tg.start_soon(functools.partial(victim, "monitor", MONITOR_PW, basic=True))

    anyio.run(scenario)
    assert [status for status, _ in victims] == ["ok"] * 8
    assert max(took for _, took in victims) < 0.3  # the queue alone holds 0.5 s
    assert flood_results.count("busy") > len(flood_results) / 2


# -- the priority lane reveals nothing and allows no unthrottled guessing -----------------------------------
def trace(name: str, scenario: str) -> list[tuple[str, int, float]]:
    """What an attacker at the shared address observes for wrong passwords of ``name``: status,
    Retry-After and response time – with a full queue, a draining queue and an empty one."""
    clock = FakeClock()
    auth, _ = make_auth(clock, timed=True)
    observed = []

    def probe(n=1):
        for _ in range(n):
            started = clock.t
            r = attempt(auth, name, "geraten")
            observed.append((r.status, r.retry_after, round(clock.t - started, 3)))

    fill_queue(auth)
    if scenario == "draining":  # the first probe (priority lane for an existing name) has to wait
        clock.advance(SOFT_QUEUE_SECONDS - 4)
        probe(2)
        fill_queue(auth)
    probe(3)  # queue full
    clock.advance(SOFT_QUEUE_SECONDS - 4)  # 4 s left in the queue
    probe()
    fill_queue(auth)
    clock.advance(SOFT_QUEUE_SECONDS - 1)
    probe(2)
    clock.advance(1000)  # flood over: the per-name brake decides
    for _ in range(PAIR.free + 2):
        probe()
        clock.advance(SOFT_SPACING_CAP)
    return observed


@pytest.mark.parametrize("scenario", ["full", "draining"])
def test_wrong_passwords_look_the_same_for_existing_and_unknown_names(scenario):
    known, unknown = trace("anna", scenario), trace("anton", scenario)
    assert known == unknown
    assert {status for status, _, _ in known} >= {"busy", "invalid", "throttled"}  # all paths covered
    assert any(status == "invalid" and took > CHECK for status, _, took in known)  # waited in the queue


def test_priority_is_one_check_per_name_not_unlimited_guessing():
    clock = FakeClock()
    auth, checked = make_auth(clock)
    for _ in range(50):  # an attacker keeps the queue full and guesses demo's password
        fill_queue(auth)
        assert attempt(auth, "demo", "geraten").status == "busy"
        clock.advance(1)
    # a check or two on the priority lane, the rest turned away unchecked
    assert checked.count("demo") == PRIORITY_CLAIMS
    assert attempt(auth, "anna", PW).ok  # colleagues keep their own priority


def test_parallel_guesses_get_no_more_priority_checks():
    clock = FakeClock()
    auth, checked = make_auth(clock)
    fill_queue(auth)
    statuses = []

    async def burst():
        async def one():
            statuses.append((await auth.authenticate("demo", "geraten", SHARED)).status)

        async with anyio.create_task_group() as tg:
            for _ in range(30):
                tg.start_soon(one)

    anyio.run(burst)
    assert statuses == ["busy"] * 30 and checked.count("demo") == PRIORITY_CLAIMS


def test_own_typo_ends_the_priority_until_success():
    """No recent failure for the name from this address – a typo of one's own uses it up (the
    attempt is counted like every other); the next successful login restores it."""
    clock = FakeClock()
    auth, _ = make_auth(clock)
    assert attempt(auth, "demo", "vertippt").status == "invalid"  # queue empty: counted, answered at once
    fill_queue(auth)
    assert attempt(auth, "demo", PW).status == "busy"  # now queued behind the flood like anybody
    clock.advance(SOFT_QUEUE_SECONDS + 1)
    assert attempt(auth, "demo", PW).ok
    fill_queue(auth)
    assert attempt(auth, "demo", PW).ok  # priority back after the successful login


def test_targeted_failures_still_throttle_hard():
    clock = FakeClock()
    auth, _ = make_auth(clock)
    for _ in range(PAIR.free):
        clock.advance(auth.throttle.retry_after(auth._throttle_keys("anna", SHARED, None)))
        attempt(auth, "anna", "geraten")
    assert attempt(auth, "anna", PW).status == "throttled"
    assert attempt(auth, "demo", PW).ok


def test_device_cookie_path_unchanged():
    clock = FakeClock()
    auth, _ = make_auth(clock)
    device = auth.make_device_cookie("demo")
    fill_queue(auth)
    assert attempt(auth, "demo", PW, device=device).ok


def test_claim_and_release():
    clock = FakeClock()
    throttle = LoginThrottle(clock=clock, forget_after=900)
    assert throttle.claim("pair:a|x", "prio:a|x") and not throttle.claim("pair:a|x", "prio:a|x")
    throttle.release("prio:a|x")
    assert throttle.claim("pair:a|x", "prio:a|x")
    clock.advance(901)
    assert throttle.claim("pair:a|x", "prio:a|x")  # forgotten
    assert throttle.claim("pair:a|x", "prio:a|x", limit=2) and not throttle.claim("pair:a|x", "prio:a|x", limit=2)
    throttle.success(["prio:a|x"])
    assert throttle.claim("pair:a|x", "prio:a|x")  # a success clears all claims
    throttle.failure({"pair:a|y": PAIR})
    assert not throttle.claim("pair:a|y", "prio:a|y")  # a recent failure for the name: no claim
    assert throttle.retry_after({"prio:a|x": PAIR}) == 0  # a marker never brakes


def test_check_credentials_uses_the_same_rules():
    clock = FakeClock()
    auth, _ = make_auth(clock)
    auth._sync_sleep = clock.advance
    fill_queue(auth)
    assert not auth.check_credentials("zufall", "falsch", SHARED)
    assert auth.check_credentials("demo", PW, SHARED)


# -- end to end through the application (shared address "testclient") -------------------------------------
@pytest.fixture()
def client(tmp_path):
    users = ",".join(f"{name}:{encoded}" for name, encoded in USERS.items())
    s = load_settings({"DATA_DIR": str(tmp_path), "APP_ENV": "test", "SECRET_KEY": KEY, "AUTH_USERS": users})
    app = create_app(s, background=False)
    clock = FakeClock()
    app.state.auth.throttle = LoginThrottle(clock=clock)
    app.state.auth._sleep = clock.sleep
    app.state.clock = clock
    return TestClient(app)


def form_login(client, user, password):
    return client.post("/login", data={"username": user, "password": password, "next": "/"},
                       follow_redirects=False)


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_flood_end_to_end(client):
    auth = client.app.state.auth
    assert form_login(TestClient(client.app), "anna", "vertippt").status_code == 401  # anna's own typo
    fill_queue(auth, "testclient")
    clock = client.app.state.clock
    started = clock.t
    victim = TestClient(client.app)  # new browser, no device cookie
    r = form_login(victim, "demo", PW)
    assert r.status_code == 303 and r.cookies.get(DEVICE_COOKIE)
    assert TestClient(client.app).get("/health/deps", headers=basic("monitor", MONITOR_PW)).status_code == 200
    assert TestClient(client.app).get("/api/status", headers=basic("monitor", MONITOR_PW)).status_code == 200
    assert clock.t - started < 0.1
    r = form_login(TestClient(client.app), "zufall", "falsch")  # the flooder
    assert r.status_code == 503 and r.headers["Retry-After"] == "5" and "sehr viele Anmeldungen" in r.text
    assert TestClient(client.app).get("/api/version", headers=basic("skript", "falsch")).status_code == 503
    r = form_login(TestClient(client.app), "anna", PW)  # after her own typo: queued like anybody
    assert r.status_code == 503
