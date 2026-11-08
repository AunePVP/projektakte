"""Minimal JSON-over-HTTP helper (standard library only).

Proxy environment variables are deliberately ignored: the AI services and plMail live in the
local network, and nothing may leave it by accident through a configured proxy.
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class AIServiceError(Exception):
    """A service is unreachable, too slow or answered with something we cannot use."""


class HTTPStatusError(AIServiceError):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} von {url}: {body[:200]}")
        self.status = status
        self.body = body


def request(
    method: str,
    url: str,
    *,
    payload: Any = None,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Raw request; returns (body, headers). Raises AIServiceError / HTTPStatusError."""
    data = None
    hdrs = {"Accept": "application/json", "User-Agent": "projektakte"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise HTTPStatusError(exc.code, body, url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
            raise AIServiceError(f"Zeitüberschreitung nach {timeout:g} s: {url}") from None
        raise AIServiceError(f"nicht erreichbar: {url} ({reason})") from None


def request_json(method: str, url: str, **kwargs) -> Any:
    body, _ = request(method, url, **kwargs)
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AIServiceError(f"keine gültige JSON-Antwort von {url}") from None


def post_json(url: str, payload: Any, **kwargs) -> Any:
    return request_json("POST", url, payload=payload, **kwargs)


def get_json(url: str, **kwargs) -> Any:
    return request_json("GET", url, **kwargs)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


class Timer:
    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.ms = round((time.perf_counter() - self.start) * 1000, 1)
        return False
