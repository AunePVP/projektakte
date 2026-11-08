"""Question answering end-to-end against the fakes: answers quote the expected facts and cite
sources; unknown things give "nicht gefunden"; an LLM outage gives a clean message."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.ask import NOT_FOUND_TEXT, ask, build_prompt, cited_numbers
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app

from .conftest import fake_settings
from .test_search import matches

EVAL = json.loads((Path(__file__).parent / "eval.json").read_text(encoding="utf-8"))


def test_cited_numbers():
    assert cited_numbers("A [1], B [2, 3] und C [3-5] sowie [9]", 5) == [1, 2, 3, 4, 5]
    assert cited_numbers("ohne Quelle", 3) == []


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("ask")
    generate(tmp / "demo")
    s = fake_settings(fake_ai, tmp, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return s, ai, conn


@pytest.mark.parametrize("q", EVAL, ids=lambda q: q["id"])
def test_eval_answers(env, q):
    s, ai, conn = env
    r = ask(conn, ai, str(s.db_path), q["question"], project=q["project"])
    if q.get("not_found"):
        assert r.found is False and r.answer == NOT_FOUND_TEXT and r.sources == []
        return
    assert r.found and r.status == "answered", r.answer
    assert "<think>" not in r.answer
    assert any(a.lower() in r.answer.lower() for a in q["answer_contains"]), r.answer
    # every cited source was in the prompt, and at least one cited source is an expected one
    assert r.sources and all(src in r.context for src in r.sources)
    cited_chunks = {src["chunk_id"] for src in r.sources}
    from projektakte.search import load_hits
    hits = load_hits(conn, list(cited_chunks)).values()
    assert any(matches(conn, h, src, q) for h in hits for src in q["sources"]), [x["label"] for x in r.sources]


def test_prompt_numbers_sources_and_respects_budget(env):
    s, ai, conn = env
    from projektakte.search import hybrid_search
    hits = hybrid_search(conn, ai, str(s.db_path), "Rauchableitung", project="BS-2024-0137", k=6).hits
    prompt, used = build_prompt("Frage?", hits, max_chars=1500)
    assert prompt.startswith("Quellen:\n\n[1] ") and prompt.endswith("Frage: Frage?")
    assert 1 <= len(used) < len(hits)


def test_llm_outage_returns_passages(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    run_index(s, AIServices.from_settings(s))
    fresh_fake_ai.options["fail"] = ["chat"]
    client = TestClient(create_app(s))
    r = client.post("/api/ask", json={"question": "Wie hoch ist der Löschwasserbedarf?",
                                      "project": "BS-2023-0777"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "llm_unavailable" and "KI-Dienst nicht erreichbar" in data["answer"]
    assert data["found"] is None and data["sources"]


def test_answer_without_citation_is_flagged(env, monkeypatch):
    s, ai, conn = env
    from projektakte.ai.llm import ChatResult
    monkeypatch.setattr(ai.llm, "chat", lambda *a, **k: ChatResult("Es sind 192 m³/h.", "", {}, "m", 1.0))
    r = ask(conn, ai, str(s.db_path), "Löschwasserbedarf Industriehalle?", project="BS-2023-0777")
    assert r.status == "no_citation" and r.notes and r.sources == r.context


def test_ask_api(env):
    s, _, _ = env
    client = TestClient(create_app(s))
    data = client.post("/api/ask", json={"question": "Was fordert Auflage 7 der Baugenehmigung?",
                                         "project": "BS-2024-0137"}).json()
    assert data["found"] and "Außentreppe" in data["answer"]
    assert data["sources"][0]["label"].startswith("01 Genehmigung/Baugenehmigung.pdf, S. 2")
    assert data["usage"]["total_tokens"] > 0
    cross = client.post("/api/ask", json={"question": "Für welches Bauvorhaben wird ein "
                                                      "Löschwasserteich mit Saugstelle gebaut?"}).json()
    assert cross["found"] and cross["sources"][0]["project"] == "BS-2023-0777"
    assert client.post("/api/ask", json={"question": "  "}).status_code == 400
    assert client.post("/api/ask", json={"question": "x", "project": "BS-1999-0001"}).status_code == 404
