"""Client of the page model: the office's own service that sorts a page image into a type and writes out text
pages (Nemotron Parse with a type head, repository nemotron-tune, ``eigenbau/server.py``).

``POST {url}/chat/completions`` in the OpenAI format with exactly one image as a ``data:`` URL; the service has a
fixed prompt (text in the request is ignored) and limits repetition loops of the model itself. The answer's
``content`` starts with ``TYP: Text|Plan|Foto|Leer|Unsicher``; only a text page has the model's raw text below
(blocks with coordinates and a class, see seitentext.py). Beside ``choices`` the answer carries ``kmw``:
``{"typ", "kopf", "sicherheit", "tokens", "finish", "ms", "bild", "schleife", "schleifen"}``.

A request without an image is answered "OK" (the reachability check). Errors: 400 for an image the service cannot
take (final: :class:`PageRejected`), 504 time limit, 500 GPU failure – those are tried again later.

Used by the phase "Bildauswertung" in the app process only (vision.VisionPass), never by the extraction worker.
"""
from __future__ import annotations

import base64
import re
import threading
import time
from dataclasses import dataclass

from .http import AIServiceError, HTTPStatusError, Timer, bearer, get_json, post_json

TYPES = ("Text", "Plan", "Foto", "Leer", "Unsicher")
_BY_LOWER = {t.lower(): t for t in TYPES}
_TYPE_LINE = re.compile(r"\s*TYP\s*:\s*([^\r\n]*)\r?\n?", re.IGNORECASE)


class PageRejected(AIServiceError):
    """The service cannot take this image (HTTP 400): asking again would end the same."""


@dataclass
class PageReading:
    typ: str  # one of TYPES
    kopf: str | None  # the type head's guess (what an "Unsicher" page probably is)
    sicherheit: float | None
    raw: str  # the model's raw text without the TYP line ("" unless a text page)
    schleife: bool  # the service cut a repetition loop short (the text holds a note where)
    finish: str | None  # stop | length | …
    ms: int


def _known(value) -> str | None:
    return _BY_LOWER.get(str(value or "").strip().lower())


def reading_from(data) -> PageReading:
    """The service's answer (parsed JSON) as a PageReading. The type comes from the TYP line, else from
    ``kmw.typ``; an answer without a known type is an error (the page is tried again later, no type guessed)."""
    try:
        content = data["choices"][0]["message"]["content"] or ""
        kmw = data.get("kmw") or {}
        if not isinstance(content, str) or not isinstance(kmw, dict):
            raise TypeError("content/kmw")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AIServiceError(f"unerwartetes Antwortformat des Seitenmodells: {exc!r}") from None
    line = _TYPE_LINE.match(content)
    typ = _known(line.group(1)) if line else None
    raw = content[line.end():] if line else content
    if typ is None and not line:
        typ, raw = _known(kmw.get("typ")), ""
    if typ is None:
        raise AIServiceError(f"Seitenmodell: Antwort ohne bekannten Seitentyp ({content[:60]!r})")
    confidence = kmw.get("sicherheit")
    ms = kmw.get("ms")
    return PageReading(
        typ=typ, kopf=_known(kmw.get("kopf")),
        sicherheit=float(confidence) if isinstance(confidence, (int, float)) else None,
        raw=raw.strip() if typ == "Text" else "", schleife=bool(kmw.get("schleife")),
        finish=str(kmw.get("finish") or data["choices"][0].get("finish_reason") or "") or None,
        ms=int(ms) if isinstance(ms, (int, float)) else 0)


class SeitenmodellClient:
    def __init__(self, url: str, model: str, *, timeout: float = 600.0, api_key: str = ""):
        self.url = url.rstrip("/")
        self.model = model
        # one page, waiting in the service's queue included
        self.timeout = timeout
        self._headers = bearer(api_key)
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_ok_at: float | None = None  # last page answered (the status page: busy, not down)

    def _note_error(self, msg: str) -> None:
        with self._lock:
            self.last_error, self.last_error_at = msg, time.time()

    def read_page(self, image: bytes, mime: str) -> PageReading:
        """Type and, for a text page, raw text of one page image. Raises PageRejected for an image the service
        refuses (final), AIServiceError for anything else (later again)."""
        url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        payload = {"model": self.model,
                   "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]}
        try:
            try:
                data = post_json(f"{self.url}/chat/completions", payload, timeout=self.timeout,
                                 headers=self._headers)
            except HTTPStatusError as exc:
                if exc.status == 400:
                    raise PageRejected(f"vom Seitenmodell abgelehnt: {exc}") from None
                raise
            reading = reading_from(data)
        except AIServiceError as exc:
            self._note_error(str(exc))
            raise
        self.last_ok_at = time.time()
        return reading

    def probe(self, timeout: float) -> dict:
        """Reachability for /health/deps: a request without an image must be answered "OK". The model list and
        the service's own /health (places, queue, counters) are added as information."""
        info: dict = {"url": self.url, "model": self.model}
        try:
            models = get_json(f"{self.url}/models", timeout=timeout, headers=self._headers)
            ids = [m.get("id") for m in (models.get("data") or []) if isinstance(m, dict)]
            info["models_available"] = ids
            info["model_listed"] = self.model in ids
            if ids and self.model not in ids:
                info["hint"] = f"Modell {self.model} steht nicht in der Modellliste des Dienstes"
        except AIServiceError as exc:
            info["models_error"] = str(exc)
        except (AttributeError, TypeError) as exc:
            info["models_error"] = f"unerwartetes Format der Modellliste: {exc}"
        try:
            with Timer() as t:
                data = post_json(f"{self.url}/chat/completions",
                                 {"model": self.model, "messages": [{"role": "user", "content": "Sag OK."}]},
                                 timeout=timeout, headers=self._headers)
            sample = str(data["choices"][0]["message"]["content"] or "")
            if not sample.strip().upper().startswith("OK"):
                raise AIServiceError(f"unerwartete Antwort auf die Testanfrage: {sample[:40]!r}")
            info.update(ok=True, format="seitenmodell", latency_ms=t.ms, sample=sample[:40])
        except AIServiceError as exc:
            info.update(ok=False, format=None, error=str(exc))
        except (KeyError, IndexError, AttributeError, TypeError) as exc:
            info.update(ok=False, format=None, error=f"unerwartetes Antwortformat: {exc!r}")
        base = self.url[:-3] if self.url.endswith("/v1") else self.url
        try:
            health = get_json(f"{base}/health", timeout=timeout, headers=self._headers)
            if isinstance(health, dict):
                info["dienst"] = health
        except AIServiceError as exc:
            info["dienst_error"] = str(exc)
        return info
