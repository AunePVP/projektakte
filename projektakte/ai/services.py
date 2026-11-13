"""Build the AI clients from the settings – the only place that knows where services live.

``AI_MODE=fake`` starts the fake services inside this process (once) and points all URLs at
them; otherwise the URLs from the environment are used unchanged.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from ..config import Settings
from .embeddings import EmbeddingClient
from .llm import LLMClient
from .rerank import RerankClient

log = logging.getLogger("projektakte.ai")
_fake_lock = threading.Lock()
# A service whose test request ran out of time but that answered real work (pages, embeddings, questions)
# this recently is busy, not down: its queue is full of our own requests (the status page says "ausgelastet").
BUSY_SECONDS = 120.0
_fake_server = None


def resolve_settings(settings: Settings) -> Settings:
    """Apply AI_MODE: with ``fake`` the in-process fake services replace the configured URLs."""
    global _fake_server
    if settings.ai_mode != "fake":
        return settings
    from ..fake_ai import start_background

    with _fake_lock:
        if _fake_server is None:
            _fake_server = start_background(models=[settings.llm_model])
            log.info("AI_MODE=fake: Fake-KI-Dienste laufen auf %s", _fake_server.base_url)
        base = _fake_server.base_url
    return replace(settings, llm_url=f"{base}/v1", embed_url=base, rerank_url=f"{base}/rerank")


@dataclass
class AIServices:
    llm: LLMClient
    embed: EmbeddingClient
    rerank: RerankClient
    health_timeout: float = 3.0

    @classmethod
    def from_settings(cls, s: Settings) -> "AIServices":
        return cls(
            llm=LLMClient(s.llm_url, s.llm_model, timeout=s.llm_timeout,
                          max_tokens=s.llm_max_tokens, vision=s.llm_vision,
                          api_key=s.llm_api_key, extra_body=s.llm_extra_body,
                          retry_body=getattr(s, "vision_retry_body", None)),
            embed=EmbeddingClient(s.embed_url, s.embed_model, fmt=s.embed_format,
                                  timeout=s.embed_timeout, batch_size=s.embed_batch,
                                  query_instruction=s.embed_query_instruction,
                                  api_key=s.embed_api_key),
            rerank=RerankClient(s.rerank_url, model=s.rerank_model, fmt=s.rerank_format,
                                timeout=s.rerank_timeout, api_key=s.rerank_api_key),
            health_timeout=s.health_timeout,
        )

    def check(self) -> dict:
        """Probe all three services in parallel with a short timeout (for /health/deps)."""
        t = self.health_timeout
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                "llm": pool.submit(self.llm.probe, t),
                "embeddings": pool.submit(self.embed.probe, t),
                "reranker": pool.submit(self.rerank.probe, t),
            }
            services = {name: f.result() for name, f in futures.items()}
        # what happened during normal operation (e.g. reranker fallback in search)
        now = time.time()
        for name, client in (("llm", self.llm), ("embeddings", self.embed),
                             ("reranker", self.rerank)):
            services[name]["last_error"] = client.last_error
            services[name]["last_error_at"] = client.last_error_at
            busy = busy_note(services[name], getattr(client, "last_ok_at", None), now, t)
            if busy:
                services[name].update(ok=True, busy=True, note=busy)
                services[name].pop("error", None)
        if not services["reranker"]["ok"]:
            services["reranker"]["fallback"] = "Suche ohne Reranker (Reihenfolge der hybriden Suche)"
        if not services["embeddings"]["ok"]:
            services["embeddings"]["fallback"] = "nur Volltextsuche; Vektoren werden nachgeholt"
        if not services["llm"]["ok"]:
            services["llm"]["fallback"] = "Fragen: 'KI-Dienst nicht erreichbar', Suche geht weiter"
        ok = all(s["ok"] for s in services.values())
        return {"status": "ok" if ok else "degraded", "services": services}


def busy_note(info: dict, last_ok_at: float | None, now: float, timeout: float) -> str | None:
    """The note for a service that is busy rather than down: its test request timed out, but it answered real
    work within BUSY_SECONDS. None otherwise (refused, an error answer, or no recent answer: really down)."""
    if info.get("ok") or "Zeitüberschreitung" not in str(info.get("error") or ""):
        return None
    if last_ok_at is None or now - last_ok_at > BUSY_SECONDS:
        return None
    from .http import seconds_text

    return (f"ausgelastet: Testanfrage nach {seconds_text(timeout)} s ohne Antwort, die letzte echte Anfrage "
            f"beantwortete der Dienst vor {int(max(0.0, now - last_ok_at))} s")

