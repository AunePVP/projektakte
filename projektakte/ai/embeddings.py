"""Client for the embedding service – OpenAI ``/v1/embeddings`` and TEI ``/embed``.

The format is detected at runtime (first call or probe) unless EMBED_FORMAT forces one.
Vectors are L2-normalised, so similarity is the dot product.
"""
from __future__ import annotations

import math
import threading
import time

from .http import AIServiceError, CallBudget, HTTPStatusError, Timer, bearer, post_json

FORMATS = ("openai", "tei")


def normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else list(vec)


class EmbeddingClient:
    def __init__(
        self,
        url: str,
        model: str,
        *,
        fmt: str = "auto",
        timeout: float = 60.0,
        batch_size: int = 32,
        query_instruction: str = "",
        api_key: str = "",
    ):
        base = url.rstrip("/")
        # tolerate EMBED_URL given with the OpenAI "/v1" suffix
        if base.endswith("/v1"):
            base = base[:-3]
        self.base = base
        self.model = model
        self.forced = fmt if fmt in FORMATS else None
        self.format: str | None = self.forced
        self.timeout = timeout
        self.batch_size = batch_size
        self.query_instruction = query_instruction
        self._headers = bearer(api_key)
        self._lock = threading.Lock()
        self.dim: int | None = None
        self.last_error: str | None = None
        self.last_error_at: float | None = None

    # -- formats -----------------------------------------------------------------------------
    def _call(self, fmt: str, texts: list[str], timeout: float) -> list[list[float]]:
        if fmt == "openai":
            data = post_json(
                f"{self.base}/v1/embeddings",
                {"model": self.model, "input": texts},
                timeout=timeout,
                headers=self._headers,
            )
            items = data.get("data") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise AIServiceError("Embedding-Antwort ohne 'data' (kein OpenAI-Format)")
            items = sorted(items, key=lambda d: d.get("index", 0))
            vecs = [d.get("embedding") for d in items]
        else:
            data = post_json(
                f"{self.base}/embed",
                {"inputs": texts, "normalize": True, "truncate": True},
                timeout=timeout,
                headers=self._headers,
            )
            vecs = data
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise AIServiceError("Embedding-Antwort passt nicht zur Anfrage (Anzahl)")
        out = []
        for v in vecs:
            if not isinstance(v, list) or not v or not all(isinstance(x, (int, float)) for x in v):
                raise AIServiceError("Embedding-Antwort enthält keinen Zahlenvektor")
            out.append(normalize([float(x) for x in v]))
        return out

    def _detect_and_call(self, texts: list[str], timeout: float) -> list[list[float]]:
        budget = CallBudget(timeout, self.base)  # all calls of a detection together: at most ``timeout``
        if self.format:
            try:
                return self._call(self.format, texts, budget.left())
            except HTTPStatusError as exc:
                # server may have been swapped (different format) – re-detect unless forced
                if self.forced or exc.status not in (404, 405, 415, 422):
                    raise
        errors = []
        for fmt in FORMATS:
            try:
                vecs = self._call(fmt, texts, budget.left())
                self.format = fmt
                return vecs
            except AIServiceError as exc:
                errors.append(f"{fmt}: {exc}")
                if not isinstance(exc, HTTPStatusError) and "Antwort" not in str(exc):
                    break  # unreachable/timeout – trying the other path won't help
        raise AIServiceError("; ".join(errors))

    # -- public API ----------------------------------------------------------------------------
    def embed(self, texts: list[str], *, timeout: float | None = None) -> list[list[float]]:
        """Embed document texts in batches."""
        out: list[list[float]] = []
        try:
            for i in range(0, len(texts), self.batch_size):
                batch = [t if t.strip() else " " for t in texts[i : i + self.batch_size]]
                out.extend(self._detect_and_call(batch, timeout or self.timeout))
        except AIServiceError as exc:
            with self._lock:
                self.last_error, self.last_error_at = str(exc), time.time()
            raise
        if out:
            self.dim = len(out[0])
        return out

    def embed_query(self, query: str, *, timeout: float | None = None) -> list[float]:
        """Search queries get the instruction prefix recommended for Qwen3-Embedding."""
        return self.embed([f"{self.query_instruction}{query}"], timeout=timeout)[0]

    def probe(self, timeout: float) -> dict:
        info: dict = {"url": self.base, "model": self.model, "forced_format": self.forced}
        try:
            with Timer() as t:
                vec = self._detect_and_call(["Rauchableitung im Treppenraum"], timeout)
            info.update(ok=True, format=self.format, dim=len(vec[0]), latency_ms=t.ms)
        except AIServiceError as exc:
            info.update(ok=False, format=None, error=str(exc))
        return info
