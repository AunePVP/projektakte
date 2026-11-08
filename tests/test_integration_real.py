"""Integration test against the REAL AI services (VISION.md section 7) – explicitly marked and
skipped in the normal test suite. Run it in staging/production with:

    PROJEKTAKTE_INTEGRATION=1 LLM_URL=… EMBED_URL=… RERANK_URL=… sh scripts/test.sh -m integration
"""
import os

import pytest

from projektakte.ai.services import AIServices
from projektakte.config import load_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PROJEKTAKTE_INTEGRATION") != "1",
                       reason="nur mit PROJEKTAKTE_INTEGRATION=1 gegen die echten KI-Dienste"),
]


@pytest.fixture(scope="module")
def ai():
    s = load_settings()
    assert s.ai_mode != "fake", "Integrationstest braucht AI_MODE=real"
    return AIServices.from_settings(s)


def test_formats_are_recognised(ai):
    result = ai.check()
    for name, svc in result["services"].items():
        assert svc["ok"], f"{name}: {svc.get('error')}"
    assert result["services"]["reranker"]["plausible"], "Reranker sortiert offensichtlich falsch"


def test_embeddings_are_semantic(ai):
    a, b, c = ai.embed.embed(["Rauchableitung im Treppenraum", "Rauchabzug im Treppenhaus",
                              "Rechnung für Büromaterial"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))  # noqa: E731
    assert dot(a, b) > dot(a, c)


def test_llm_answers_without_think_tags(ai):
    res = ai.llm.chat([{"role": "user", "content": "Antworte nur mit dem Wort OK."}], max_tokens=200)
    assert "<think>" not in res.text and "ok" in res.text.lower()
