"""Client for the reranker – TEI style (``texts`` → list) and Cohere/Jina style (``documents`` →
``results``). Request and response formats are detected independently at runtime."""
from __future__ import annotations

import threading
import time

from .http import AIServiceError, CallBudget, HTTPStatusError, Timer, bearer, post_json

FORMATS = ("tei", "cohere")
MAX_CHARS = 2000  # rerankers truncate anyway; keep requests small


def parse_rerank_response(data, n: int) -> tuple[list[float], str]:
    """Return one score per input document (index order) and the detected response format."""
    if isinstance(data, dict) and isinstance(data.get("results"), list):
        items, fmt = data["results"], "cohere"
    elif isinstance(data, list):
        items, fmt = data, "tei"
    else:
        raise AIServiceError("unbekanntes Reranker-Antwortformat")
    scores = [float("-inf")] * n
    for item in items:
        if not isinstance(item, dict) or "index" not in item:
            raise AIServiceError("Reranker-Antwort ohne 'index'")
        score = item.get("relevance_score", item.get("score"))
        idx = int(item["index"])
        if score is None or not 0 <= idx < n:
            raise AIServiceError("Reranker-Antwort ohne gültigen Score/Index")
        scores[idx] = float(score)
    return scores, fmt


class RerankClient:
    def __init__(
        self,
        url: str,
        *,
        model: str = "",
        fmt: str = "auto",
        timeout: float = 30.0,
        api_key: str = "",
    ):
        self.url = url.rstrip("/")
        self.model = model
        self.forced = fmt if fmt in FORMATS else None
        self.format: str | None = self.forced
        self.response_format: str | None = None
        self.timeout = timeout
        self._headers = bearer(api_key)
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_error_at: float | None = None
        self.last_ok_at: float | None = None

    def _payload(self, fmt: str, query: str, texts: list[str]) -> dict:
        if fmt == "tei":
            p = {"query": query, "texts": texts, "truncate": True}
        else:
            p = {"query": query, "documents": texts, "top_n": len(texts)}
        if self.model:
            p["model"] = self.model
        return p

    def _call(self, fmt: str, query: str, texts: list[str], timeout: float) -> list[float]:
        data = post_json(self.url, self._payload(fmt, query, texts), timeout=timeout,
                         headers=self._headers)
        scores, resp_fmt = parse_rerank_response(data, len(texts))
        self.response_format = resp_fmt
        return scores

    def rerank(self, query: str, texts: list[str], *, timeout: float | None = None) -> list[float]:
        """Scores per text (same order as ``texts``). Raises AIServiceError on failure."""
        if not texts:
            return []
        texts = [t[:MAX_CHARS] for t in texts]
        timeout = timeout or self.timeout
        try:
            scores = self._detect_and_call(query, texts, timeout)
        except AIServiceError as exc:
            with self._lock:
                self.last_error, self.last_error_at = str(exc), time.time()
            raise
        self.last_ok_at = time.time()
        return scores

    def _detect_and_call(self, query: str, texts: list[str], timeout: float) -> list[float]:
        budget = CallBudget(timeout, self.url)  # all calls of a detection together: at most ``timeout``
        if self.format:
            try:
                return self._call(self.format, query, texts, budget.left())
            except HTTPStatusError as exc:
                if self.forced or exc.status not in (400, 404, 413, 415, 422):
                    raise
            except AIServiceError as exc:
                # only an unexpected answer shape means "server swapped, detect again"; a timeout or an
                # unreachable server would otherwise be waited for twice
                if self.forced or "Antwort" not in str(exc):
                    raise
        errors = []
        for fmt in FORMATS:
            try:
                scores = self._call(fmt, query, texts, budget.left())
                self.format = fmt
                return scores
            except AIServiceError as exc:
                errors.append(f"{fmt}: {exc}")
                if not isinstance(exc, HTTPStatusError) and "Antwort" not in str(exc):
                    break
        raise AIServiceError("; ".join(errors))

    def probe(self, timeout: float) -> dict:
        info: dict = {"url": self.url, "forced_format": self.forced}
        try:
            with Timer() as t:
                scores = self._detect_and_call(
                    "Rauchableitung", ["Kantine und Speiseplan", "Rauchableitung im Treppenraum"],
                    timeout,
                )
            info.update(
                ok=True,
                format=self.format,
                response_format=self.response_format,
                latency_ms=t.ms,
                plausible=scores[1] > scores[0],
            )
        except AIServiceError as exc:
            info.update(ok=False, format=None, error=str(exc))
        return info
