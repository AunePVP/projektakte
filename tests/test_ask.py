"""Question answering end-to-end against the fakes: answers quote the expected facts and cite
sources; unknown things give "nicht gefunden" (with the found passages as context); the wording of
real models (refusals, citation formats, cut-off output) is interpreted correctly."""
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte.ai.llm import ChatResult
from projektakte.ai.services import AIServices
from projektakte.ask import (NOT_FOUND_TEXT, TRUNCATED_TEXT, ask, build_prompt, cited_numbers, diversify_projects,
                             is_not_found, normalize_citations, prefer_recent, source_header)
from projektakte.demo import generate
from projektakte.evaluate import check_answer
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.search import Hit, hybrid_search

from .conftest import fake_settings

EVAL = json.loads((Path(__file__).parent / "eval.json").read_text(encoding="utf-8"))


def test_cited_numbers():
    assert cited_numbers("A [1], B [2, 3] und C [3-5] sowie [9]", 5) == [1, 2, 3, 4, 5]
    assert cited_numbers("ohne Quelle", 3) == []
    assert cited_numbers("Die Betriebsdauer beträgt 8 Stunden [Quelle 2].", 6) == [2]
    assert cited_numbers("Laut Quelle 3 und (Quellen 1 und 4) gilt das.", 6) == [3, 1, 4]
    assert cited_numbers("Siehe [1–3] und [2][5].", 6) == [1, 2, 3, 5]
    assert cited_numbers("Fußnote[^2].", 6) == [2]
    assert cited_numbers("Im Jahr [2024] und Auflage (4).", 6) == []


def test_normalize_citations():
    assert normalize_citations("8 Stunden [Quelle 2] und (Quellen 1 und 3), [1–3].", 6) == \
        "8 Stunden [2] und [1, 3], [1, 2, 3]."
    assert normalize_citations("laut Quelle 2 gilt", 6) == "laut Quelle 2 gilt"  # prose stays


@pytest.mark.parametrize("answer", [
    "Nicht gefunden.", "**Nicht gefunden.**", "Leider nicht gefunden.",
    "Die Quellen enthalten keine Angaben zu Fahrradstellplätzen.",
    "In den vorliegenden Unterlagen finden sich keine Informationen zu Fahrradstellplätzen mit Ladestation.",
    "Dazu liegen keine Informationen vor.", "Die Frage lässt sich anhand der Quellen nicht beantworten.",
    "In den Quellen ist keine Sprinkleranlage erwähnt.", "Hierzu enthalten die Quellen keine Aussage [2].",
    "Das geht aus den Unterlagen nicht hervor.", "Das ist in den Quellen nicht genannt.",
    # round 2: further common refusals of real models
    "Ich konnte in den Quellen nichts dazu finden.", "Es gibt in den Quellen keinen Hinweis auf eine Sprinkleranlage.",
    "Darüber geben die Quellen keine Auskunft.", "Das kann ich anhand der vorliegenden Quellen nicht sagen.",
])
def test_refusals_of_real_models_are_not_found(answer):
    assert is_not_found(answer, 6)


@pytest.mark.parametrize("answer", [
    "Keine Abweichungen beantragt [2].", "Eine Sprinkleranlage ist nicht vorgesehen [3].",
    "Die Kosten sind in der Baugenehmigung nicht enthalten [2].",
    "Zu Fahrradstellplätzen gibt es keine Angaben. Die Sicherheitsbeleuchtung muss 8 Stunden halten [1].",
    "Die Betriebsdauer beträgt 8 Stunden [1].",
    # round 2: cited negative facts are answers, not refusals (in fire safety they are the news)
    "Es liegen keine Abweichungen vor [1].", "Es liegen keine Mängel mehr vor [1].",
    "Laut Abnahmeprotokoll vom 12.12.2024 liegen keine Mängel mehr vor [1].",
    "Seitens der Feuerwehr liegen keine Bedenken vor [3].",
    "Der Prüfbericht enthält keine Hinweise auf Mängel an der Feststellanlage [2].",
    "Die Quellen nennen keinen Termin, die Abnahme ist aber für März vorgesehen [2].",
    "Laut Prüfbericht Nr. 3 liegen keine offenen Mängel mehr vor [3].",
    "Laut Mail vom 03.04.2024 gibt es keine Antwort des Bauherrn auf die Mängelliste [5].",
])
def test_answers_are_not_mistaken_for_refusals(answer):
    assert not is_not_found(answer, 6)


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
    chk = check_answer(conn, q, r)
    assert chk.passed, (chk.reasons, r.status, r.answer, [x["label"] for x in r.sources])
    assert "<think>" not in r.answer
    if q.get("not_found"):
        assert r.found is False and r.answer == NOT_FOUND_TEXT and r.sources == []
    else:
        assert r.found is True and all(src in r.context for src in r.sources)


def _patch_answer(monkeypatch, ai, text, raw=None, finish="stop", reasoning=""):
    monkeypatch.setattr(ai.llm, "chat", lambda *a, **k: ChatResult(text, raw if raw is not None else text, {},
                                                                   "m", 1.0, finish, reasoning))


def test_prompt_numbers_sources_dates_and_respects_budget(env):
    s, ai, conn = env
    hits = hybrid_search(conn, ai, str(s.db_path), "Rauchableitung", project="BS-2024-0137", k=6).hits
    prompt, used = build_prompt("Frage?", hits, max_chars=1500)
    assert prompt.startswith("Quellen:\n\n[1] ") and prompt.endswith("Frage: Frage?")
    assert 1 <= len(used) < len(hits)
    doc = next(h for h in hits if h.kind == "document")
    assert "(Stand " in source_header(1, doc) and source_header(1, doc).endswith("Grundschule Am Lindenhof")
    mail = hybrid_search(conn, ai, str(s.db_path), "aktueller Stand", project="BS-2023-0412", k=10).hits
    assert any(h.kind == "mail" and "Mail vom " in source_header(2, h) for h in mail)


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
    assert data["found"] is None and data["sources"] == [] and data["context"]
    assert all("n" in c for c in data["context"])
    # the user sees what happened, not the service URL or the raw error (that goes to the log)
    assert not any("http" in n or "127.0.0.1" in n for n in data["notes"]), data["notes"]


@pytest.mark.parametrize("quirk", ["reasoning_only", "unterminated_think"])
def test_cut_off_reasoning_is_truncated_not_not_found(tmp_path, fresh_fake_ai, quirk):
    """ops-04: a thinking model that used up the tokens must not produce a false "nicht gefunden"."""
    generate(tmp_path / "demo")
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    fresh_fake_ai.options["chat_quirk"] = quirk
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    r = ask(conn, ai, str(s.db_path), "Welche Betriebsdauer muss die Sicherheitsbeleuchtung haben?",
            project="BS-2024-0233")
    assert r.status == "truncated" and r.answer == TRUNCATED_TEXT and r.found is None
    assert r.context and r.sources == [] and any("LLM_MAX_TOKENS" in n for n in r.notes)
    fresh_fake_ai.options["chat_quirk"] = "length"  # cut off in the middle of the answer
    r = ask(conn, ai, str(s.db_path), "Welche Betriebsdauer muss die Sicherheitsbeleuchtung haben?",
            project="BS-2024-0233")
    assert r.status == "truncated" and r.answer.endswith("…") and r.context and r.found is None


def test_answer_without_citation_is_unbelegt(env, monkeypatch):
    s, ai, conn = env
    _patch_answer(monkeypatch, ai, "Es sind 192 m³/h.")
    r = ask(conn, ai, str(s.db_path), "Löschwasserbedarf Industriehalle?", project="BS-2023-0777")
    assert r.status == "no_citation" and r.sources == [] and r.context and r.found is None
    assert r.answer == "Es sind 192 m³/h." and any("unbelegt" in n for n in r.notes)


def test_free_refusal_is_not_found_with_context(env, monkeypatch):
    s, ai, conn = env
    _patch_answer(monkeypatch, ai, "Die Quellen enthalten keine Angaben zur Kantinenmiete.")
    r = ask(conn, ai, str(s.db_path), "Wie hoch ist die Kantinenmiete im Hotel Seeblick?", project="BS-2024-0233")
    assert r.status == "not_found" and r.found is False and r.answer == NOT_FOUND_TEXT
    assert r.sources == [] and r.context  # A2: the found passages stay visible


def test_other_citation_formats_count(env, monkeypatch):
    s, ai, conn = env
    _patch_answer(monkeypatch, ai, "Die Betriebsdauer beträgt 8 Stunden [Quelle 1].")
    r = ask(conn, ai, str(s.db_path), "Welche Betriebsdauer muss die Sicherheitsbeleuchtung haben?",
            project="BS-2024-0233")
    assert r.status == "answered" and r.answer.endswith("[1].") and [x["n"] for x in r.sources] == [1]


def test_nothing_relevant_gives_not_found_with_best_hits(env):
    s, ai, conn = env
    r = ask(conn, ai, str(s.db_path), "Welche Betriebsdauer muss die Sicherheitsbeleuchtung haben?",
            project="BS-2024-0233", min_rerank=5.0)
    assert r.status == "not_found" and r.answer == NOT_FOUND_TEXT and r.usage == {}
    assert 1 <= len(r.context) <= 3 and r.notes and "passt gut genug" in r.notes[-1]
    assert "ASK_MIN_RERANK" not in r.notes[-1]  # no setting names in user-visible text


def test_overlapping_chunks_are_merged_in_the_prompt(env):
    """A7: the same numbered condition must not reach the model twice (chunk overlap)."""
    s, ai, conn = env
    r = ask(conn, ai, str(s.db_path), "Welche Auflagen zur Rauchableitung stehen in der Baugenehmigung?",
            project="BS-2024-0137")
    paragraphs = [p for c in r.context for p in c["text"].split("\n") if p.startswith("Auflage")]
    assert len(paragraphs) == len(set(paragraphs))
    assert r.answer.count("Auflage 4") == 1


def test_enumeration_spreads_over_projects(env):
    """A5: 'Welche Projekte …?' gets the best passage of every project, with a hint."""
    s, ai, conn = env
    r = ask(conn, ai, str(s.db_path), "Welche Projekte hat Frau Wendehals geprüft?")
    assert len({c["project"] for c in r.context} - {None}) == 8
    assert any("vollständige Liste" in n for n in r.notes)


def _hit(i, project, date=None):
    return Hit(chunk_id=i, project=project, project_name=None, kind="mail", title=f"t{i}", doc_id=None, mail_id=i,
               rel_path=None, page=None, heading=None, date=date, sender=None, text=f"Text {i}")


def test_diversify_and_prefer_recent():
    hits = [_hit(1, "A"), _hit(2, "A"), _hit(3, "B"), _hit(4, "A"), _hit(5, "C")]
    assert [h.chunk_id for h in diversify_projects(hits, 4)] == [1, 3, 5, 2]
    dated = [_hit(i, "A", f"2024-0{i}-01") for i in range(1, 10)]
    picked = prefer_recent(dated, 4)
    assert [h.chunk_id for h in picked] == [1, 2, 8, 9]  # two best plus the two newest, in rank order


def test_ask_api(env):
    s, _, _ = env
    client = TestClient(create_app(s))
    data = client.post("/api/ask", json={"question": "Was fordert Auflage 7 der Baugenehmigung?",
                                         "project": "BS-2024-0137"}).json()
    assert data["status"] == "answered" and data["found"] and "Außentreppe" in data["answer"]
    assert data["sources"][0]["label"].startswith("01 Genehmigung/Baugenehmigung.pdf, S. 2")
    assert data["usage"]["total_tokens"] > 0
    cross = client.post("/api/ask", json={"question": "Für welches Bauvorhaben wird ein "
                                                      "Löschwasserteich mit Saugstelle gebaut?"}).json()
    assert cross["found"] and cross["sources"][0]["project"] == "BS-2023-0777"
    assert "BS-2023-0777" in cross["answer"]
    nf = client.post("/api/ask", json={"question": "Wie viele Fahrradstellplätze hat das Hotel Seeblick?"}).json()
    assert nf["status"] == "not_found" and nf["sources"] == [] and "context" in nf
    assert client.post("/api/ask", json={"question": "  "}).status_code == 400
    assert client.post("/api/ask", json={"question": "x", "project": "BS-1999-0001"}).status_code == 404
