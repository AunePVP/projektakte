"""Hybrid search with reranker; the evaluation set must have its expected source in the top 5."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.search import fts_query, hybrid_search, rrf

from .conftest import fake_settings

EVAL = json.loads((Path(__file__).parent / "eval.json").read_text(encoding="utf-8"))


def test_fts_query_and_rrf():
    assert fts_query("Welche Auflagen zur Rauchableitung?") == '"auflag"* OR "rauchableitung"*'
    assert fts_query("Wer? Was?") is None
    assert '"7"' in fts_query("Auflage 7")
    assert rrf([(1, 9.0), (2, 8.0)], [(2, 0.9), (3, 0.8)])[0][0] == 2


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("search")
    generate(tmp / "demo")
    s = fake_settings(fake_ai, tmp, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return s, ai, conn


def matches(conn, hit, src, q) -> bool:
    project = src.get("project") or q["project"]
    if "file" in src:
        return hit.kind == "document" and hit.project == project and hit.rel_path == src["file"]
    if "mail_subject" in src:
        if not hit.mail_id:
            return False
        return conn.execute("SELECT subject FROM mails WHERE id = ?", (hit.mail_id,)).fetchone()[0] == \
            src["mail_subject"]
    return hit.project == project


@pytest.mark.parametrize("q", [q for q in EVAL if q["sources"]], ids=lambda q: q["id"])
def test_eval_expected_source_in_top5(env, q):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), q["question"], project=q["project"], k=5)
    assert res.reranked
    assert any(matches(conn, h, src, q) for h in res.hits for src in q["sources"]), \
        [h.label for h in res.hits]


def test_project_filter(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Rettungsweg", project="BS-2022-0901", k=10)
    assert res.hits and {h.project for h in res.hits} == {"BS-2022-0901"}


def test_labels_name_file_page_or_mail_date(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Auflage 7 Außentreppe", project="BS-2024-0137", k=3)
    assert res.hits[0].label == "01 Genehmigung/Baugenehmigung.pdf, S. 2"
    res = hybrid_search(conn, ai, str(s.db_path), "Zustimmung Abweichung Flurlänge", project="BS-2024-0137")
    mail = next(h for h in res.hits if h.kind == "mail")
    assert mail.label.startswith("Mail vom 18.06.2024 (Dipl.-Ing. Sabine Wendehals)")


def test_reranker_outage_falls_back(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    fresh_fake_ai.options["fail"] = ["rerank"]
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    res = hybrid_search(conn, ai, str(s.db_path), "Löschwasserbedarf Industriehalle", k=5)
    assert not res.reranked and res.hits and "Reranker" in res.notes[0]
    # /health/deps reports the reranker problem and the fallback
    deps = TestClient(create_app(s)).get("/health/deps").json()["services"]["reranker"]
    assert deps["ok"] is False and "fallback" in deps
    # embeddings down too: full text alone still answers
    fresh_fake_ai.options["fail"] = ["rerank", "embed"]
    res = hybrid_search(conn, ai, str(s.db_path), "Löschwasserbedarf Industriehalle", k=5)
    assert res.hits and any("Vektorsuche" in n for n in res.notes)


def test_search_api(env):
    s, _, _ = env
    client = TestClient(create_app(s))
    data = client.get("/api/search", params={"q": "Feuerwehraufzug Vorraum", "project": "bs-2024-0311"}).json()
    assert data["project"] == "BS-2024-0311" and data["reranked"] and len(data["hits"]) <= 10
    assert data["hits"][0]["label"] and data["hits"][0]["text"]
    cross = client.get("/api/search", params={"q": "Löschwasserteich Saugstelle", "k": 3}).json()
    assert cross["hits"][0]["project"] == "BS-2023-0777"
    assert client.get("/api/search", params={"q": " "}).status_code == 400
    assert client.get("/api/search", params={"q": "x", "project": "BS-1999-0001"}).status_code == 404
