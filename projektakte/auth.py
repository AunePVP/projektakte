"""Access control: users from the configuration (PBKDF2 password hashes), signed session cookie.

* ``AUTH_USERS="anna:pbkdf2_sha256$…,ben:pbkdf2_sha256$…"`` and/or ``AUTH_USERS_FILE`` (one
  ``name:hash`` per line) – create entries with ``python -m projektakte.passwd anna``.
* ``SECRET_KEY`` (random, at least 32 characters) signs the cookies. Without it, a random key is
  used and sessions end with every restart. In ``APP_ENV=production`` a missing or weak key stops
  the start-up; elsewhere it is logged as a warning.
* Session cookie: user, issue time (``iat``), expiry (``exp``) and the user's *session epoch*.
  The expiry is bounded server-side by ``SESSION_HOURS`` (``iat`` + session length). Logging out
  increments the epoch (``meta`` table, key ``session_epoch:<user>``), which ends every session of
  that user on every device – a copied cookie is useless afterwards.
* Failed logins slow down exponentially instead of locking an account (see "login throttling"):
  failures for one user name – from one address or from anywhere – make further attempts for that
  name wait (rejected without a password check). Failures of *other* names from the same address
  (name spraying, a shared terminal server/NAT address) only space attempts out by at most
  ``SOFT_SPACING_CAP`` seconds; a correct password still gets through. A browser that logged in
  successfully before carries a signed device cookie and is throttled on its own; HTTP Basic
  clients (scripts, monitoring) are only rejected after failures for their own user name from
  their own address. At most a few PBKDF2 checks run at the same time; waiting attempts hold no
  worker thread. The failure table is bounded and cleaned up.
* Everything except ``/health``, ``/health/ready``, ``/login``, ``/favicon.ico`` and ``/static``
  requires a login. API clients may also send HTTP Basic credentials.
* ``AUTH_DISABLED=1`` switches the check off – only with ``APP_ENV=dev`` (``test`` for the
  automated tests). Any other environment refuses to start.
* ``COOKIE_SECURE``: if not set, on in ``APP_ENV=production`` (TLS reverse proxy expected).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Awaitable, Callable, Iterable, Mapping

import anyio
import anyio.to_thread

from . import db
from .passwords import DEFAULT_ITERATIONS, hash_password, verify_password  # noqa: F401 (re-exported)
from .passwords import b64 as _b64, unb64 as _unb64
from .startup import ConfigError

log = logging.getLogger("projektakte.auth")

COOKIE = "projektakte_session"
DEVICE_COOKIE = "projektakte_device"
PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {"/health", "/health/ready", "/login", "/favicon.ico"}
# allowlist: environments in which AUTH_DISABLED=1 is accepted ("test" = automated test suite)
AUTH_DISABLED_ENVS = frozenset({"dev", "test"})
MIN_SECRET_KEY_LENGTH = 32
MIN_SECRET_KEY_CHARS = 10  # distinct characters – rejects "aaaa…" and similar
MAX_SESSION_HOURS = 24 * 30
DEVICE_SECONDS = 180 * 86400
CLOCK_SKEW = 60  # seconds a cookie's issue time may lie in the future (clock differences)
MAX_COOKIE_LENGTH = 4096
MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_LENGTH = 1024
VERIFY_SLOTS = max(1, min(4, (os.cpu_count() or 2) // 2))  # concurrent PBKDF2 checks
VERIFY_QUEUE_SECONDS = 10.0  # longer waits for a slot are answered with "busy"
SOFT_SPACING_CAP = 2.0  # seconds: the most a "soft" brake spaces out attempts of one key
SOFT_QUEUE_SECONDS = 20.0  # longer queues behind a soft brake (a flood from one address) → "busy"
BUSY_LOG_INTERVAL = 60.0  # seconds between two "busy" warnings in the log (no log flood)
KEY_HINT = 'Neuen Schlüssel erzeugen: python -c "import secrets; print(secrets.token_urlsafe(48))"'

_TOKEN = re.compile(r"[A-Za-z0-9_-]+")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    """Verified for unknown user names, so that their response time matches known ones."""
    if not _DUMMY:
        _DUMMY.append(hash_password("dummy", salt=b"0" * 16))
    return _DUMMY[0]


def parse_users(spec: str, file: str | None = None, *, strict: bool = False) -> dict[str, str]:
    """``name:hash`` entries separated by commas/newlines (env) or one per line (file).

    An unreadable ``file`` is logged – or, with ``strict`` (production), raises ConfigError: the
    application would run "healthy" without anybody being able to log in."""
    users: dict[str, str] = {}
    chunks = spec.replace("\n", ",").split(",")
    if file:
        try:
            chunks += Path(file).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "strerror", None) or type(exc).__name__
            if strict:
                raise ConfigError(
                    f"AUTH_USERS_FILE '{file}' ist nicht lesbar ({reason}). Im Container die Datei einbinden, "
                    f"z. B. -v /pfad/benutzer.txt:{file}:ro, und für die Benutzer-ID der Anwendung "
                    f"lesbar machen (Image: 10001, z. B. 'sudo chown 10001:10001 benutzer.txt && "
                    f"sudo chmod 600 benutzer.txt').") from None
            log.error("AUTH_USERS_FILE nicht lesbar: %s", exc)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        name, _, encoded = chunk.partition(":")
        if name.strip() and encoded.strip().startswith("pbkdf2_sha256$"):
            users[name.strip()] = encoded.strip()
        else:
            log.error("Ungültiger Benutzereintrag ignoriert (Name: %r)", name.strip())
    return users


def parse_basic(header: str | None) -> tuple[str, str] | None:
    """(user, password) from an ``Authorization: Basic …`` header, None if absent or malformed."""
    if not header or header[:6].lower() != "basic ":
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, binascii.Error):  # includes UnicodeDecodeError and non-ASCII input
        return None
    user, sep, password = decoded.partition(":")
    return (user, password) if sep else None


def client_key(host: str | None) -> str:
    """Throttling key for a client address: IPv6 clients by their /64 network (one device can use
    many addresses of it), IPv4-mapped addresses as IPv4."""
    try:
        ip = ipaddress.ip_address(host or "")
    except ValueError:
        return host or "-"
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


# -- configuration policy ---------------------------------------------------------------------------
def check_auth_disabled(disabled: bool, app_env: str) -> bool:
    """AUTH_DISABLED=1 is only accepted in the allowlisted environments – otherwise ConfigError."""
    env = (app_env or "").strip().lower()
    if disabled and env not in AUTH_DISABLED_ENVS:
        raise ConfigError(
            f"AUTH_DISABLED=1 ist nur mit APP_ENV=dev erlaubt (aktuell APP_ENV={env or '(leer)'}). "
            "Ohne Zugangsschutz wären alle Mails und Projektunterlagen für jeden im Netz lesbar. "
            "Für eine lokale Vorführung APP_ENV=dev setzen, sonst AUTH_DISABLED entfernen und "
            "Benutzer mit 'python -m projektakte.passwd NAME' anlegen (AUTH_USERS).")
    return disabled


def secret_key_problem(key: str) -> str | None:
    """German description of what is wrong with SECRET_KEY, or None if it looks strong enough."""
    if not key:
        return "SECRET_KEY ist nicht gesetzt"
    if len(key) < MIN_SECRET_KEY_LENGTH:
        return f"SECRET_KEY ist kürzer als {MIN_SECRET_KEY_LENGTH} Zeichen"
    if len(set(key)) < MIN_SECRET_KEY_CHARS:
        return "SECRET_KEY besteht aus zu wenigen verschiedenen Zeichen"
    return None


def check_secret_key(key: str, app_env: str) -> None:
    """Weak keys allow forging session cookies: refused in production, a warning elsewhere."""
    problem = secret_key_problem(key)
    if not problem:
        return
    if (app_env or "").strip().lower() == "production":
        raise ConfigError(f"{problem}. In APP_ENV=production ist ein zufälliger SECRET_KEY mit mindestens "
                          f"{MIN_SECRET_KEY_LENGTH} Zeichen Pflicht, sonst lassen sich Anmeldungen fälschen "
                          f"bzw. enden mit jedem Neustart. {KEY_HINT}")
    if key:  # a missing key is reported by Auth() itself (random key per start)
        log.warning("%s – Sitzungs-Cookies lassen sich damit fälschen. %s", problem, KEY_HINT)


def resolve_cookie_secure(s, environ: Mapping[str, str] | None = None) -> bool:
    """COOKIE_SECURE if set explicitly (``Settings.cookie_secure`` True/False), otherwise
    (``None``) on in ``APP_ENV=production``. ``environ`` is accepted for backwards compatibility."""
    production = (s.app_env or "").strip().lower() == "production"
    value = s.cookie_secure
    return production if value is None else bool(value)


def bounded_session_hours(hours: float) -> float:
    if hours <= 0:
        log.warning("SESSION_HOURS=%s ungültig – 12 Stunden werden verwendet", hours)
        return 12.0
    if hours > MAX_SESSION_HOURS:
        log.warning("SESSION_HOURS=%s zu groß – auf %d Stunden begrenzt", hours, MAX_SESSION_HOURS)
        return float(MAX_SESSION_HOURS)
    return hours


# -- login throttling -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Backoff:
    """Delay after the n-th failure: none for the first ``free - 1`` ones, then doubling up to ``cap``.

    ``hard``: while the delay lasts, attempts are rejected without a password check (429). A *soft*
    brake never rejects a correct password: attempts only queue up – spaced ``spacing()`` apart – and
    are then checked normally. Only when the queue grows beyond SOFT_QUEUE_SECONDS (a flood) are
    further attempts turned away as "busy", like any overload."""
    free: int
    base: float
    cap: float
    hard: bool = True

    def delay(self, failures: int) -> float:
        if failures < self.free:
            return 0.0
        # bounded exponent: under a soft brake the count keeps growing (2.0 ** 1024 overflows)
        return min(self.cap, self.base * 2.0 ** min(failures - self.free, 60))

    def spacing(self, failures: int) -> float:
        """Minimum distance between two attempts of a key under this (soft) brake."""
        return min(SOFT_SPACING_CAP, self.delay(failures))


PAIR = Backoff(free=5, base=2.0, cap=300.0)     # one client, one user name
USER = Backoff(free=10, base=1.0, cap=60.0)     # one user name from anywhere (distributed guessing)
# one client, any user names (name spraying, load): soft – behind a shared address (terminal server,
# NAT, proxy without FORWARDED_ALLOW_IPS) it would otherwise hit every colleague
CLIENT = Backoff(free=20, base=0.25, cap=SOFT_SPACING_CAP, hard=False)
DEVICE = Backoff(free=5, base=2.0, cap=300.0)   # a browser that logged in before (device cookie)
# HTTP Basic (scripts, monitoring): failures for the user name elsewhere only slow it down
USER_SOFT = replace(USER, hard=False)


@dataclass
class _Strikes:
    count: int = 0
    last: float = 0.0
    until: float = 0.0       # hard brake: no attempt before this time
    next_free: float = 0.0   # soft brake: the next attempt's place in the queue


class LoginThrottle:
    """Failure counters with exponential back-off instead of a hard lock-out.

    Attempts during a (hard) wait are rejected without being counted, so a wait never grows by
    itself and always ends. Counters are forgotten ``forget_after`` seconds after the last failure;
    the table holds at most ``max_entries`` keys (least recently failed ones are dropped first)."""

    def __init__(self, *, max_entries: int = 10_000, forget_after: float = 900.0,
                 clock: Callable[[], float] = time.monotonic):
        self.max_entries = max_entries
        self.forget_after = forget_after
        self._clock = clock
        self._entries: OrderedDict[str, _Strikes] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._entries)

    def retry_after(self, keys: Iterable[str] | Mapping[str, Backoff]) -> float:
        """Seconds until all of ``keys`` may try again (0 = now). For a mapping key → policy, only
        the hard brakes count (soft ones never reject)."""
        if isinstance(keys, Mapping):
            keys = [key for key, policy in keys.items() if policy.hard]
        now = self._clock()
        with self._lock:
            waits = [e.until - now for k in keys if (e := self._entries.get(k)) is not None]
        return max([0.0, *waits])

    def reserve(self, policies: Mapping[str, Backoff], max_wait: float) -> float | None:
        """Takes the next place in the queue of every soft brake in ``policies`` that is active and
        returns how long this attempt has to wait for it (0 = now). Attempts of such a key are thus
        spaced at least ``policy.spacing(failures)`` apart, however many arrive in parallel. None –
        and nothing reserved – if the wait would exceed ``max_wait``."""
        now = self._clock()
        with self._lock:
            queued: list[tuple[_Strikes, float]] = []
            start = now
            for key, policy in policies.items():
                entry = self._entries.get(key)
                if policy.hard or entry is None or now - entry.last > self.forget_after:
                    continue
                spacing = policy.spacing(entry.count)
                if spacing <= 0 and entry.next_free <= now:
                    continue
                queued.append((entry, spacing))
                start = max(start, entry.next_free)
            if start - now > max_wait:
                return None
            for entry, spacing in queued:
                entry.next_free = start + spacing
        return start - now

    def failure(self, policies: Mapping[str, Backoff]) -> tuple[float, list[tuple[str, int]]]:
        """Counts a failure for every key. Returns the resulting (hard) wait and the keys whose
        throttling just started (with their failure count) for logging."""
        now = self._clock()
        wait, started = 0.0, []
        with self._lock:
            self._forget_stale(now)
            for key, policy in policies.items():
                entry = self._entries.pop(key, None)
                if entry is None or now - entry.last > self.forget_after:
                    entry = _Strikes()
                entry.count += 1
                entry.last = now
                delay = policy.delay(entry.count)
                # also for a soft use of the key: the same key may be checked hard elsewhere
                # (user:<name> – soft for Basic clients, hard for the login form)
                entry.until = now + delay
                self._entries[key] = entry  # most recent failure at the end
                if delay and entry.count == policy.free:
                    started.append((key, entry.count))
                if policy.hard:
                    wait = max(wait, delay)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
        return wait, started

    def success(self, keys: Iterable[str]) -> None:
        with self._lock:
            for key in keys:
                self._entries.pop(key, None)

    def _forget_stale(self, now: float) -> None:
        # entries are ordered by their last failure, and a wait (≤ cap) ends long before forget_after
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if now - entry.last <= self.forget_after:
                break
            del self._entries[key]


@dataclass(frozen=True)
class LoginResult:
    status: str  # "ok" | "invalid" | "throttled" (too many failures) | "busy" (too many parallel checks)
    user: str | None = None
    retry_after: int = 0  # seconds until the next attempt makes sense (0 = immediately)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# -- session epochs ---------------------------------------------------------------------------------
def _parse_epoch(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


class SessionEpochs:
    """Per-user session generation. Every cookie carries the epoch it was issued in; logging out
    increments it, which invalidates all cookies issued before – on every device.

    Stored in the ``meta`` table (survives restarts; no schema change); without a database (tests,
    scripts) it lives in memory. Values read from the database are cached for ``ttl`` seconds;
    changes made by this process take effect immediately."""

    PREFIX = "session_epoch:"

    def __init__(self, db_path: Path | str | None = None, *, ttl: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        self.db_path = db_path
        self.ttl = ttl
        self._clock = clock
        self._cache: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()
        self._schema_ready = False

    def current(self, user: str) -> int | None:
        """The user's current epoch; None if it cannot be read (callers fail closed)."""
        now = self._clock()
        with self._lock:
            hit = self._cache.get(user)
        if self.db_path is None:
            return hit[0] if hit else 0
        if hit and now - hit[1] < self.ttl:
            return hit[0]
        try:
            with closing(self._connect()) as conn:
                value = _parse_epoch(db.get_meta(conn, self.PREFIX + user))
        except sqlite3.Error as exc:
            log.error("Sitzungsstand nicht lesbar: %s", exc)
            return hit[0] if hit else None
        with self._lock:
            self._cache[user] = (value, now)
        return value

    def bump(self, user: str) -> int:
        """Ends all sessions of ``user``; returns the new epoch. Raises sqlite3.Error on failure."""
        if self.db_path is None:
            with self._lock:
                value = self._cache.get(user, (0, 0.0))[0] + 1
                self._cache[user] = (value, self._clock())
            return value
        with closing(self._connect(write=True)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            value = _parse_epoch(db.get_meta(conn, self.PREFIX + user)) + 1
            db.set_meta(conn, self.PREFIX + user, str(value))
            conn.commit()
        with self._lock:
            self._cache[user] = (value, self._clock())
        return value

    def _connect(self, *, write: bool = False) -> sqlite3.Connection:
        if not self._schema_ready:
            db.init_db(self.db_path)  # idempotent; a no-op once the app has initialised the database
            self._schema_ready = True
        conn = db.connect(self.db_path)  # writes wait up to 30 s for the indexer (logout runs in a thread)
        if not write:
            conn.execute("PRAGMA busy_timeout = 5000")  # read on the request path: never wait long
        return conn


# -- the access control object ----------------------------------------------------------------------
class Auth:
    def __init__(self, users: dict[str, str], secret_key: str, session_hours: float = 12.0,
                 disabled: bool = False, cookie_secure: bool = False, *,
                 epochs: SessionEpochs | None = None, throttle: LoginThrottle | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] | None = None):
        self.users = users
        if not secret_key:
            if not disabled:
                log.warning("SECRET_KEY nicht gesetzt – zufälliger Schlüssel, Anmeldungen gelten nur bis "
                            "zum nächsten Neustart")
            secret_key = secrets.token_hex(32)
        root = hashlib.sha256(secret_key.encode("utf-8")).digest()
        # separate keys per purpose: a device cookie can never pass as a session cookie
        self._session_key = hmac.new(root, b"projektakte/session/v2", hashlib.sha256).digest()
        self._device_key = hmac.new(root, b"projektakte/device/v1", hashlib.sha256).digest()
        self.session_seconds = int(session_hours * 3600)
        self.device_seconds = DEVICE_SECONDS
        self.disabled = disabled
        self.cookie_secure = cookie_secure
        self.epochs = epochs if epochs is not None else SessionEpochs()
        self.throttle = throttle if throttle is not None else LoginThrottle()
        self._clock = clock
        self._sleep = sleep or anyio.sleep  # waiting behind a soft brake (tests: a fake clock)
        self._sync_sleep: Callable[[float], None] = time.sleep  # check_credentials()
        self._slots = anyio.Semaphore(VERIFY_SLOTS)
        self._known_names = {name.lower() for name in users}
        self._busy_logged = -BUSY_LOG_INTERVAL

    @classmethod
    def from_settings(cls, s) -> "Auth":
        """Applies the configuration policy (raises ConfigError for unsafe combinations)."""
        env = (s.app_env or "").strip().lower()
        disabled = check_auth_disabled(s.auth_disabled, env)
        strict = env == "production" and not disabled
        users = parse_users(s.auth_users, s.auth_users_file or None, strict=strict)
        secure = resolve_cookie_secure(s)
        if disabled:
            log.warning("Zugangsschutz ausgeschaltet (AUTH_DISABLED=1, APP_ENV=%s) – nur für die Entwicklung!",
                        env)
        else:
            check_secret_key(s.secret_key, env)
            if not users and strict:
                raise ConfigError("Keine Benutzer konfiguriert (AUTH_USERS bzw. AUTH_USERS_FILE) – niemand "
                                  "könnte sich anmelden. Benutzer mit 'python -m projektakte.passwd NAME' anlegen "
                                  "und die ausgegebene Zeile in AUTH_USERS oder in die Datei unter "
                                  "AUTH_USERS_FILE eintragen (Docker: die Datei mit "
                                  "-v /pfad/benutzer.txt:/run/secrets/users:ro einbinden).")
            if not users:
                log.warning("Keine Benutzer konfiguriert (AUTH_USERS/AUTH_USERS_FILE) – Anmeldung unmöglich")
            if env == "production" and not secure:
                log.warning("APP_ENV=production mit COOKIE_SECURE=0: Sitzungs-Cookies gehen auch über "
                            "unverschlüsseltes HTTP – nur hinter einem TLS-Reverse-Proxy betreiben "
                            "(siehe deploy/) und COOKIE_SECURE nicht abschalten")
        return cls(users, s.secret_key, bounded_session_hours(s.session_hours), disabled, secure,
                   epochs=SessionEpochs(s.db_path))

    # -- passwords --------------------------------------------------------------------------------
    async def authenticate(self, user: str, password: str, client: str = "",
                           device: str | None = None, *, basic: bool = False) -> LoginResult:
        """Checks a login attempt (form, or HTTP Basic with ``basic=True``). Attempts behind a soft
        brake wait (asynchronously) for their turn. The PBKDF2 work runs in a worker thread, at most
        VERIFY_SLOTS at a time; waiting for a slot holds no thread."""
        keys = self._throttle_keys(user, client, device, basic=basic)
        if blocked := self._blocked(keys):
            return blocked
        delay = self.throttle.reserve(keys, SOFT_QUEUE_SECONDS)
        if delay is None:
            return self._busy("zu viele Anmeldeversuche von derselben Adresse")
        if delay > 0:
            await self._sleep(delay)
        try:
            with anyio.fail_after(VERIFY_QUEUE_SECONDS):
                await self._slots.acquire()
        except TimeoutError:
            return self._busy("zu viele gleichzeitige Passwortprüfungen")
        try:
            if blocked := self._blocked(keys):  # failures counted while this attempt was queued
                return blocked
            ok = await anyio.to_thread.run_sync(self._verify, user, password)
        finally:
            self._slots.release()
        return self._record(keys, user, ok)

    async def authenticate_basic(self, header: str | None, client: str = "") -> LoginResult:
        creds = parse_basic(header)
        if creds is None:
            return LoginResult("invalid")
        return await self.authenticate(creds[0], creds[1], client, basic=True)

    def check_credentials(self, user: str, password: str, client: str = "", *, basic: bool = False) -> bool:
        """Synchronous variant (scripts, tests): same throttling (waits behind a soft brake by
        sleeping), no concurrency limit."""
        keys = self._throttle_keys(user, client, None, basic=basic)
        if self._blocked(keys):
            return False
        delay = self.throttle.reserve(keys, SOFT_QUEUE_SECONDS)
        if delay is None:
            return False
        if delay > 0:
            self._sync_sleep(delay)
        return self._record(keys, user, self._verify(user, password)).ok

    def basic_user(self, header: str | None, client: str = "") -> str | None:
        creds = parse_basic(header)
        return creds[0] if creds and self.check_credentials(creds[0], creds[1], client, basic=True) else None

    def _verify(self, user: str, password: str) -> bool:
        encoded = self.users.get(user)
        if len(user) > MAX_USERNAME_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
            encoded = None  # still hash (the dummy) so the response time gives nothing away
        ok = verify_password(password[:MAX_PASSWORD_LENGTH], encoded or _dummy_hash())
        return ok and encoded is not None

    def _throttle_keys(self, user: str, client: str, device: str | None, *,
                       basic: bool = False) -> dict[str, Backoff]:
        """Brakes for one attempt (key → policy). Only failures for this user name can reject a
        correct password: from this address (pair), and – for the login form – from anywhere (user).
        Failures of other names from this address (client) never reject, they only space attempts
        out; HTTP Basic clients are spaced out by failures for their name elsewhere, too."""
        name = user.lower()[:MAX_USERNAME_LENGTH]
        device_id = self.device_id(device, user) if device else None
        if device_id:  # a browser this user logged in with before: not affected by others' failures
            return {f"device:{device_id}": DEVICE}
        host = client_key(client)
        return {f"pair:{host}|{name}": PAIR, f"user:{name}": USER_SOFT if basic else USER,
                f"client:{host}": CLIENT}

    def _blocked(self, keys: Mapping[str, Backoff]) -> LoginResult | None:
        wait = self.throttle.retry_after(keys)
        return LoginResult("throttled", retry_after=math.ceil(wait)) if wait > 0 else None

    def _busy(self, reason: str) -> LoginResult:
        """Overload: the attempt is turned away unchecked and uncounted (warned at most once a minute)."""
        now = time.monotonic()
        if now - self._busy_logged >= BUSY_LOG_INTERVAL:
            self._busy_logged = now
            log.warning("Anmeldungen abgewiesen: %s", reason)
        return LoginResult("busy", retry_after=5)

    def _record(self, keys: Mapping[str, Backoff], user: str, ok: bool) -> LoginResult:
        if ok:
            self.throttle.success(k for k in keys if not k.startswith("client:"))
            return LoginResult("ok", user=user)
        wait, started = self.throttle.failure(keys)
        for key, count in started:
            if key.startswith("client:"):  # soft for everybody (user:<name> is hard for the login form)
                log.warning("Anmeldungen werden verzögert nach %d Fehlversuchen: %s", count, self._describe(key))
            else:
                log.warning("Anmeldung gedrosselt nach %d Fehlversuchen: %s", count, self._describe(key))
        return LoginResult("invalid", retry_after=math.ceil(wait))

    def _describe(self, key: str) -> str:
        """Key for the log – unknown user names are not logged (they may be mistyped passwords)."""
        kind, _, rest = key.partition(":")
        if kind == "device":
            return f"Gerät {rest[:6]}…"
        if kind == "client":
            return f"Client {rest}"
        host, _, name = rest.rpartition("|")
        shown = name if name in self._known_names else "(unbekannter Name)"
        return f"Benutzer {shown}" + (f" von {host}" if kind == "pair" else "")

    # -- signed tokens ----------------------------------------------------------------------------
    @staticmethod
    def _sign(key: bytes, payload: str) -> str:
        return _b64(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())

    def _seal(self, key: bytes, data: dict) -> str:
        payload = _b64(json.dumps(data, separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{self._sign(key, payload)}"

    def _unseal(self, key: bytes, value: str | None) -> dict | None:
        """Payload of a signed token; None for anything malformed, forged or not a JSON object.
        Never raises – a broken cookie means "no session", not an error page."""
        if not value or len(value) > MAX_COOKIE_LENGTH or not value.isascii():
            return None
        payload, sep, sig = value.rpartition(".")
        if not sep or not _TOKEN.fullmatch(payload) or not _TOKEN.fullmatch(sig):
            return None
        if not hmac.compare_digest(sig.encode("ascii"), self._sign(key, payload).encode("ascii")):
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, binascii.Error):  # includes UnicodeDecodeError
            return None
        return data if isinstance(data, dict) else None

    # -- session cookie ---------------------------------------------------------------------------
    def make_cookie(self, user: str) -> str:
        epoch = self.epochs.current(user)
        if epoch is None:
            raise RuntimeError("Sitzungsstand nicht lesbar – Anmeldung derzeit nicht möglich")
        now = int(self._clock())
        return self._seal(self._session_key, {"v": 2, "u": user, "iat": now,
                                              "exp": now + self.session_seconds, "e": epoch})

    def read_cookie(self, value: str | None) -> str | None:
        data = self._unseal(self._session_key, value)
        if not data or data.get("v") != 2:
            return None
        user, iat, exp, epoch = data.get("u"), data.get("iat"), data.get("exp"), data.get("e")
        if not (isinstance(user, str) and _is_int(iat) and _is_int(exp) and _is_int(epoch)):
            return None
        now = self._clock()
        if iat > now + CLOCK_SKEW or now >= min(exp, iat + self.session_seconds):
            return None  # issued in the future, or expired (the server bounds the lifetime)
        if user not in self.users:
            return None  # removed from the configuration
        if self.epochs.current(user) != epoch:
            return None  # logged out since (or the session state is unreadable: fail closed)
        return user

    def logout(self, user: str) -> None:
        """Ends every session of ``user`` (all devices)."""
        self.epochs.bump(user)
        log.info("Abgemeldet: %s – alle Sitzungen beendet", user)

    # -- device cookie (login throttling only, grants no access) ----------------------------------
    def make_device_cookie(self, user: str) -> str:
        return self._seal(self._device_key, {"v": 1, "u": user.lower(), "d": secrets.token_urlsafe(12),
                                             "iat": int(self._clock())})

    def device_id(self, value: str | None, user: str) -> str | None:
        data = self._unseal(self._device_key, value)
        if not data or data.get("v") != 1:
            return None
        device, name, iat = data.get("d"), data.get("u"), data.get("iat")
        if not (isinstance(device, str) and isinstance(name, str) and _is_int(iat)):
            return None
        if name != user.lower() or not 0 <= self._clock() - iat < self.device_seconds:
            return None
        return device


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def safe_next(target: str | None) -> str:
    """Only local redirects after login (no //host, no scheme)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
