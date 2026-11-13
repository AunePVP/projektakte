"""Minimal JSON-over-HTTP helper (standard library only).

Proxy environment variables are deliberately ignored: the AI services and plMail live in the
local network, and nothing may leave it by accident through a configured proxy.
"""
from __future__ import annotations

import json
import math
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


def seconds_text(seconds: float) -> str:
    """'60', '37,5' – for messages the user may see: at most one decimal, German comma."""
    if not math.isfinite(seconds):
        return "unbegrenzt" if seconds > 0 else str(seconds)
    r = round(seconds, 1)
    return str(int(r)) if r == int(r) else f"{r:.1f}".replace(".", ",")


def timeout_error(timeout: float, url: str) -> AIServiceError:
    return AIServiceError(f"Zeitüberschreitung nach {seconds_text(timeout)} s: {url}")


class CallBudget:
    """One logical request may take several HTTP calls (format detection after an unexpected answer);
    together they never take longer than the timeout of that request."""

    MIN_SECONDS = 0.1  # less left than this: give up instead of starting another call

    def __init__(self, timeout: float, url: str):
        self.timeout, self.url = timeout, url
        self.end = time.monotonic() + timeout

    def left(self) -> float:
        rest = self.end - time.monotonic()
        if rest < self.MIN_SECONDS:
            raise timeout_error(self.timeout, self.url)
        return rest

    def own(self, exc: AIServiceError) -> AIServiceError:
        """A timeout of one call of the detection, reported with the timeout of the whole request
        (the configured one) instead of the remaining slice the call had."""
        text = str(exc)
        if isinstance(exc, HTTPStatusError) or not text.startswith("Zeitüberschreitung"):
            return exc
        return timeout_error(self.timeout, text.split(": ", 1)[1] if ": " in text else self.url)


def _open(method: str, url: str, payload: Any, timeout: float, headers: dict[str, str] | None, accept: str):
    data = None
    hdrs = {"Accept": accept, "User-Agent": "projektakte"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        raise HTTPStatusError(exc.code, body, url) from None
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise _network_error(exc, timeout, url) from None


def _network_error(exc: BaseException, timeout: float, url: str) -> AIServiceError:
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
        return timeout_error(timeout, url)
    return AIServiceError(f"nicht erreichbar: {url} ({reason})")


def request(
    method: str,
    url: str,
    *,
    payload: Any = None,
    timeout: float = 10.0,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, dict[str, str]]:
    """Raw request; returns (body, headers). Raises AIServiceError / HTTPStatusError."""
    end = time.monotonic() + timeout
    resp = _open(method, url, payload, timeout, headers, "application/json")
    try:
        with resp:
            return _read_until(resp, end, timeout, url), {k.lower(): v for k, v in resp.headers.items()}
    except AIServiceError:
        raise
    except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise _network_error(exc, timeout, url) from None


class EventStream:
    """The answer of a streaming POST (server-sent events), read line by line within the timeout of the
    whole request. A server that ignores ``"stream": true`` answers with one JSON body instead:
    :attr:`is_stream` tells which, :meth:`json` reads that body. Leaving the ``with`` block closes the
    connection – a vLLM or llama.cpp server then stops generating for this request."""

    def __init__(self, resp, end: float, timeout: float, url: str):
        self._resp, self._end, self._timeout, self.url = resp, end, timeout, url
        self.is_stream = "text/event-stream" in (resp.headers.get("Content-Type") or "").lower()

    def __enter__(self) -> "EventStream":
        return self

    def __exit__(self, *exc) -> bool:
        self._resp.close()
        return False

    def json(self) -> Any:
        try:
            body = _read_until(self._resp, self._end, self._timeout, self.url)
        except AIServiceError:
            raise
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise _network_error(exc, self._timeout, self.url) from None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AIServiceError(f"keine gültige JSON-Antwort von {self.url}") from None

    def events(self):
        """The ``data:`` payloads of the stream, decoded from JSON, until ``[DONE]`` or its end."""
        sock = getattr(getattr(getattr(self._resp, "fp", None), "raw", None), "_sock", None)
        while True:
            rest = self._end - time.monotonic()
            if rest <= 0:
                raise timeout_error(self._timeout, self.url)
            if sock is not None:
                try:
                    sock.settimeout(rest)
                except OSError:
                    sock = None
            try:
                line = self._resp.readline()
            except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
                raise _network_error(exc, self._timeout, self.url) from None
            if not line:
                return
            line = line.strip()
            if not line.startswith(b"data:"):
                continue  # comments, event names, blank separators
            data = line[5:].strip()
            if data == b"[DONE]":
                return
            try:
                yield json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise AIServiceError(f"ungültiges Ereignis im Datenstrom von {self.url}") from None


def post_stream(url: str, payload: Any, *, timeout: float, headers: dict[str, str] | None = None) -> EventStream:
    """POST that asks for a streamed answer (see :class:`EventStream`)."""
    end = time.monotonic() + timeout
    resp = _open("POST", url, payload, timeout, headers, "text/event-stream, application/json")
    return EventStream(resp, end, timeout, url)


def _read_until(resp, end: float, timeout: float, url: str) -> bytes:
    """The response body, but never past ``end``: the socket timeout alone applies to each single
    read, so a server that trickles its answer byte by byte could otherwise hold a call (and the
    question's time budget) far beyond its timeout."""
    sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    read = getattr(resp, "read1", None) or resp.read
    parts = []
    while True:
        rest = end - time.monotonic()
        if rest <= 0:
            raise timeout_error(timeout, url)
        if sock is not None:
            try:
                sock.settimeout(rest)
            except OSError:
                sock = None
        chunk = read(65536)
        if not chunk:
            return b"".join(parts)
        parts.append(chunk)


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
