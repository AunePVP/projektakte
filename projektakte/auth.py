"""Access control: users from the configuration (PBKDF2 password hashes), signed session cookie.

* ``AUTH_USERS="anna:pbkdf2_sha256$…,ben:pbkdf2_sha256$…"`` and/or ``AUTH_USERS_FILE`` (one
  ``name:hash`` per line) – create entries with ``python -m projektakte.passwd anna``.
* ``SECRET_KEY`` signs the session cookie (without it, a random key is used and sessions end
  with every restart).
* Everything except ``/health``, ``/login`` and ``/static`` requires a login. API clients may also
  send HTTP Basic credentials.
* ``AUTH_DISABLED=1`` switches the check off for local development – never in production.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path

log = logging.getLogger("projektakte.auth")

COOKIE = "projektakte_session"
DEFAULT_ITERATIONS = 600_000
PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {"/health", "/login", "/favicon.ico"}
MAX_FAILURES = 5
LOCK_SECONDS = 300


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, digest = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt), int(iterations))
        return hmac.compare_digest(calc, _unb64(digest))
    except (ValueError, binascii.Error):
        return False


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    """Verified for unknown user names, so that their response time matches known ones."""
    if not _DUMMY:
        _DUMMY.append(hash_password("dummy", salt=b"0" * 16))
    return _DUMMY[0]


def parse_users(spec: str, file: str | None = None) -> dict[str, str]:
    """``name:hash`` entries separated by commas/newlines (env) or one per line (file)."""
    users: dict[str, str] = {}
    chunks = spec.replace("\n", ",").split(",")
    if file:
        try:
            chunks += Path(file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
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


class Auth:
    def __init__(self, users: dict[str, str], secret_key: str, session_hours: float = 12.0,
                 disabled: bool = False, cookie_secure: bool = False):
        self.users = users
        if not secret_key:
            log.warning("SECRET_KEY nicht gesetzt – zufälliger Schlüssel, Anmeldungen gelten nur bis "
                        "zum nächsten Neustart")
            secret_key = secrets.token_hex(32)
        self._key = hashlib.sha256(secret_key.encode("utf-8")).digest()
        self.session_seconds = int(session_hours * 3600)
        self.disabled = disabled
        self.cookie_secure = cookie_secure
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, s) -> "Auth":
        disabled = s.auth_disabled
        if disabled and s.app_env == "production":
            log.error("AUTH_DISABLED wird in APP_ENV=production ignoriert")
            disabled = False
        users = parse_users(s.auth_users, s.auth_users_file or None)
        if disabled:
            log.warning("Zugangsschutz ausgeschaltet (AUTH_DISABLED=1) – nur für die Entwicklung!")
        elif not users:
            log.warning("Keine Benutzer konfiguriert (AUTH_USERS/AUTH_USERS_FILE) – Anmeldung unmöglich")
        return cls(users, s.secret_key, s.session_hours, disabled, s.cookie_secure)

    # -- passwords --------------------------------------------------------------------------------
    def locked(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(key, []) if now - t < LOCK_SECONDS]
            self._failures[key] = recent
            return len(recent) >= MAX_FAILURES

    def check_credentials(self, user: str, password: str, client: str = "") -> bool:
        key = f"{client}|{user.lower()}"
        if self.locked(key):
            return False
        encoded = self.users.get(user)
        ok = verify_password(password, encoded or _dummy_hash()) and encoded is not None
        if not ok:
            with self._lock:
                self._failures.setdefault(key, []).append(time.time())
        return ok

    # -- session cookie ---------------------------------------------------------------------------
    def make_cookie(self, user: str) -> str:
        payload = _b64(json.dumps({"u": user, "exp": int(time.time()) + self.session_seconds}).encode())
        sig = _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())
        return f"{payload}.{sig}"

    def read_cookie(self, value: str | None) -> str | None:
        if not value or "." not in value:
            return None
        payload, sig = value.rsplit(".", 1)
        expected = _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            data = json.loads(_unb64(payload))
        except (ValueError, binascii.Error):
            return None
        if data.get("exp", 0) < time.time() or data.get("u") not in self.users:
            return None  # expired, or the user was removed from the configuration
        return data["u"]

    def basic_user(self, header: str | None, client: str = "") -> str | None:
        if not header or not header.lower().startswith("basic "):
            return None
        try:
            user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, binascii.Error, UnicodeDecodeError):
            return None
        return user if self.check_credentials(user, password, client) else None


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def safe_next(target: str | None) -> str:
    """Only local redirects after login (no //host, no scheme)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target
