"""A service whose test request on the status page times out while it is busy with our own work (pages,
embedding batches) is "ausgelastet", not "nicht erreichbar" – only if it answered real work recently."""
from __future__ import annotations

import time

from projektakte.ai.services import BUSY_SECONDS, AIServices, busy_note

TIMEOUT = "Zeitüberschreitung nach 3 s: http://192.168.0.9:8001/v1/embeddings"


def test_busy_only_after_a_timeout_with_a_recent_answer():
    now = 1000.0
    assert busy_note({"ok": False, "error": TIMEOUT}, now - 5, now, 3.0).startswith("ausgelastet")
    assert busy_note({"ok": False, "error": TIMEOUT}, now - BUSY_SECONDS - 1, now, 3.0) is None  # long ago
    assert busy_note({"ok": False, "error": TIMEOUT}, None, now, 3.0) is None  # never answered
    refused = "nicht erreichbar: http://192.168.0.9:8001 (Connection refused)"
    assert busy_note({"ok": False, "error": refused}, now - 5, now, 3.0) is None  # really down
    assert busy_note({"ok": True}, now - 5, now, 3.0) is None


class _Client:
    def __init__(self, info: dict, last_ok_at=None):
        self.info, self.last_ok_at = info, last_ok_at
        self.last_error = self.last_error_at = None

    def probe(self, timeout):
        return dict(self.info)


def test_the_status_shows_a_busy_service_as_working():
    ok = {"ok": True, "url": "u", "latency_ms": 400.0}
    ai = AIServices(llm=_Client(ok), rerank=_Client(ok),
                    embed=_Client({"ok": False, "url": "u", "error": TIMEOUT}, last_ok_at=time.time() - 10))
    res = ai.check()
    emb = res["services"]["embeddings"]
    assert res["status"] == "ok" and emb["ok"] and emb["busy"] and "error" not in emb and "fallback" not in emb
    assert "Testanfrage nach 3 s ohne Antwort" in emb["note"]
    # without recent work it stays down, with its fallback
    ai.embed.last_ok_at = None
    res = ai.check()
    assert res["status"] == "degraded" and not res["services"]["embeddings"]["ok"]
    assert res["services"]["embeddings"]["fallback"].startswith("nur Volltextsuche")


def test_the_table_says_ausgelastet():
    from projektakte.web import templates

    deps = {"ai_mode": "real", "services": {"embeddings": {"ok": True, "busy": True, "url": "u", "note":
                                                            "ausgelastet: Testanfrage nach 3 s ohne Antwort"}}}
    html = templates.get_template("_deps.html").render(deps=deps)
    assert "ausgelastet</span>" in html and "nicht erreichbar" not in html


def test_real_clients_remember_their_last_answer(fake_ai):
    from projektakte.ai.embeddings import EmbeddingClient
    from projektakte.ai.llm import LLMClient

    embed = EmbeddingClient(fake_ai.base_url, "fake-embed")
    assert embed.last_ok_at is None
    embed.embed(["Rauchabzug"])
    assert time.time() - embed.last_ok_at < 5
    llm = LLMClient(f"{fake_ai.base_url}/v1", "fake")
    llm.chat([{"role": "user", "content": "Sag OK."}], max_tokens=8)
    assert time.time() - llm.last_ok_at < 5
