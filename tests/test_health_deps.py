from fastapi.testclient import TestClient

from projektakte.config import load_settings
from projektakte.main import create_app

from .conftest import fake_settings


def test_health_deps_reports_each_service(fake_ai, tmp_path):
    client = TestClient(create_app(fake_settings(fake_ai, tmp_path)))
    r = client.get("/health/deps")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    svc = data["services"]
    assert svc["llm"]["ok"] and svc["llm"]["format"] == "openai-chat"
    assert svc["llm"]["model_listed"] is True
    assert svc["embeddings"]["ok"] and svc["embeddings"]["format"] == "openai"
    assert svc["embeddings"]["dim"] == 256
    assert svc["reranker"]["ok"] and svc["reranker"]["format"] == "tei"
    assert svc["reranker"]["plausible"] is True
    for s in svc.values():
        assert s["latency_ms"] >= 0


def test_health_deps_reports_other_formats(fresh_fake_ai, tmp_path):
    fresh_fake_ai.options.update(embed_formats=["tei"], rerank_formats=["cohere"])
    svc = TestClient(create_app(fake_settings(fresh_fake_ai, tmp_path))).get(
        "/health/deps").json()["services"]
    assert svc["embeddings"]["format"] == "tei"
    assert svc["reranker"]["format"] == "cohere"


def test_health_stays_fast_when_services_are_down(tmp_path):
    dead = "http://127.0.0.1:9"
    s = load_settings({"DATA_DIR": str(tmp_path), "LLM_URL": dead + "/v1", "EMBED_URL": dead,
                       "RERANK_URL": dead + "/rerank", "HEALTH_TIMEOUT": "0.5"})
    client = TestClient(create_app(s))
    assert client.get("/health").json()["status"] == "ok"
    data = client.get("/health/deps").json()
    assert data["status"] == "degraded"
    assert not any(v["ok"] for v in data["services"].values())
    assert "fallback" in data["services"]["reranker"]
    assert data["services"]["llm"]["error"]


def test_ai_mode_fake_starts_inprocess_services(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "AI_MODE": "fake"})
    app = create_app(s)
    assert app.state.settings.llm_url.startswith("http://127.0.0.1:")
    data = TestClient(app).get("/health/deps").json()
    assert data["status"] == "ok" and data["ai_mode"] == "fake"
