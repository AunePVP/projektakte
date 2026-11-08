"""Hybrid search with reranker; the evaluation set must have its expected source in the top 5.
Abbreviations and synonyms are expanded in the full-text query; labels use the local date."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.evaluate import expected_rank, source_matches
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.search import Hit, fts_query, hybrid_search, merge_adjacent, rrf

from .conftest import fake_settings

EVAL = json.loads((Path(__file__).parent / "eval.json").read_text(encoding="utf-8"))


def matches(conn, hit, src, q) -> bool:  # kept for other test modules
    return source_matches(conn, hit, src, q)


def test_fts_query_and_rrf():
    assert fts_query("Welche Auflagen zur Rauchableitung?") == '"auflag"* OR "rauchableitung"*'
    assert fts_query("Wer? Was?") is None
    assert '"7"' in fts_query("Auflage 7")
    assert rrf([(1, 9.0), (2, 8.0)], [(2, 0.9), (3, 0.8)])[0][0] == 2


def test_fts_query_expands_abbreviations_synonyms_and_word_forms():
    q = fts_query("Wann wird die BMA abgenommen?", expand=True)
    assert '"bma"' in q and '"brandmeldeanlag"*' in q and '"abnahm"*' in q
    q = fts_query("Wie groß muss die Entrauchungsöffnung im Treppenhaus sein?", expand=True)
    assert '("rauchableitung"* AND "offnung"*)' in q and '"treppenraum"*' in q
    assert '"notlicht"*' in fts_query("Notlicht", expand=True) and \
        '"sicherheitsbeleuchtung"*' in fts_query("Notlicht", expand=True)
    # filler words of questions are no search terms; without expand nothing changes (assignment)
    assert "projekt" not in fts_query("In welchen Projekten ist eine Sprachalarmanlage vorgesehen?", expand=True)
    assert fts_query("Wann wird die BMA abgenommen?") == '"bma" OR "abgenomm"*'


def test_synonyms_file_extends_the_list(tmp_path):
    f = tmp_path / "synonyme.txt"
    f.write_text("# eigene Kürzel\nWEA = Wasserentnahmestelle = Saugstelle\nLP -> Löschwasserteich\n",
                 encoding="utf-8")
    from projektakte.textutil import load_synonyms
    syn = load_synonyms(str(f))
    q = fts_query("Wo ist die WEA?", expand=True, synonyms=syn)
    assert '"saugstell"*' in q and '"wasserentnahmestell"*' in q
    assert '"loschwasserteich"*' in fts_query("LP", expand=True, synonyms=syn)
    assert '"lp"' not in fts_query("Löschwasserteich", expand=True, synonyms=syn)  # one-way
    assert '"bma"' in fts_query("Brandmeldeanlage", expand=True, synonyms=syn)  # built-in list stays
    assert load_synonyms(str(tmp_path / "fehlt.txt")) == load_synonyms(None)


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


@pytest.mark.parametrize("q", [q for q in EVAL if q["sources"]], ids=lambda q: q["id"])
def test_eval_expected_source_in_top5(env, q):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), q["question"], project=q["project"], k=5)
    assert res.reranked
    rank = expected_rank(conn, q, res.hits)
    assert rank is not None and rank <= 5, [h.label for h in res.hits]


def test_search_uses_synonyms_file_setting(env, tmp_path, monkeypatch):
    s, ai, conn = env
    f = tmp_path / "syn.txt"
    f.write_text("XQZ = Saugstelle\n", encoding="utf-8")
    without = hybrid_search(conn, ai, str(s.db_path), "XQZ", synonyms_file="", k=3)
    assert not any("Saugstelle" in h.text for h in without.hits)
    monkeypatch.setenv("SYNONYMS_FILE", str(f))  # default: setting SYNONYMS_FILE
    res = hybrid_search(conn, ai, str(s.db_path), "XQZ", k=3)
    assert "Saugstelle" in res.hits[0].text


def test_project_filter(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Rettungsweg", project="BS-2022-0901", k=10)
    assert res.hits and {h.project for h in res.hits} == {"BS-2022-0901"}


def test_filler_words_do_not_decide_the_ranking(env):
    """B3: 'Projekten … vorgesehen' must not lift passages without the topic to the top."""
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "In welchen Projekten ist eine Sprachalarmanlage vorgesehen?", k=5)
    assert "Sprachalarmanlage" in res.hits[0].text
    assert all("Sprachalarm" in h.text or "sprachalarm" in h.text.lower() for h in res.hits[:3])


def test_labels_name_file_page_or_mail_date(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "Auflage 7 Außentreppe", project="BS-2024-0137", k=3)
    assert res.hits[0].label == "01 Genehmigung/Baugenehmigung.pdf, S. 2"
    res = hybrid_search(conn, ai, str(s.db_path), "Zustimmung Abweichung Flurlänge", project="BS-2024-0137")
    mail = next(h for h in res.hits if h.kind == "mail")
    assert mail.label.startswith("Mail vom 18.06.2024 (Dipl.-Ing. Sabine Wendehals)")


def _hit(**kw):
    base = dict(chunk_id=1, project="P", project_name="N", kind="mail", title="Betreff", doc_id=None, mail_id=7,
                rel_path=None, page=None, heading=None, date=None, sender="Anna", text="Text")
    base.update(kw)
    return Hit(**base)


def test_labels_use_local_date():
    """BUG-11: a mail sent at 00:30 Berlin time is stored as the previous day in UTC."""
    h = _hit(date="2024-06-17T22:30:00+00:00")
    assert h.label == "Mail vom 18.06.2024 (Anna): Betreff"
    assert h.local_date.isoformat() == "2024-06-18"
    winter = _hit(kind="attachment", doc_id=3, title="Plan.pdf", date="2024-12-31T23:30:00+00:00")
    assert winter.label == "Plan.pdf (Anhang einer Mail vom 01.01.2025)"
    assert _hit(kind="document", doc_id=3, rel_path="a/b.pdf", date="2024-11-14", page=2).label == "a/b.pdf, S. 2"


def test_merge_adjacent_joins_overlapping_chunks():
    a = _hit(chunk_id=10, kind="document", doc_id=5, mail_id=None, page=2, ord=1, rerank=0.9,
             text="Auflage 3 (X): drei.\nAuflage 4 (Y): vier.")
    b = _hit(chunk_id=11, kind="document", doc_id=5, mail_id=None, page=2, ord=2, rerank=0.7,
             text="Auflage 4 (Y): vier.\nAuflage 5 (Z): fünf.")
    other_page = _hit(chunk_id=12, kind="document", doc_id=5, mail_id=None, page=3, ord=3, text="Hinweise")
    contained = _hit(chunk_id=13, kind="document", doc_id=5, mail_id=None, page=2, ord=7, text="Auflage 4 (Y): vier.")
    merged = merge_adjacent([a, other_page, b, contained])
    assert [h.chunk_id for h in merged] == [10, 12]
    assert merged[0].text == "Auflage 3 (X): drei.\nAuflage 4 (Y): vier.\nAuflage 5 (Z): fünf."
    assert merged[0].rerank == 0.9 and merged[0].ord == 1
    # neighbours that arrive in reverse order are joined in document order
    merged = merge_adjacent([b, a])
    assert merged[0].chunk_id == 11 and merged[0].text.startswith("Auflage 3")


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
