"""PBKDF2 password hashes – standard library only, so ``python -m projektakte.passwd`` runs with
any Python 3 (no third-party packages needed on the admin's machine)."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

DEFAULT_ITERATIONS = 600_000


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${b64(salt)}${b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt, digest = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), unb64(salt), int(iterations))
        return hmac.compare_digest(calc, unb64(digest))
    except (ValueError, binascii.Error):
        return False
