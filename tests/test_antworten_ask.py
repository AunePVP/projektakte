"""Answers over the demo data (round 2): snippets show the quoted passage as clean text, user texts
carry no URLs or setting names, latest-state and summary questions get the right context, and
reference codes (project number, file number) find their documents first."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from projektakte.ai import AIServiceError
from projektakte.ai.llm import ChatResult
from projektakte.ai.services import AIServices
from projektakte.ask import answer_segments, ask, llm_error_note
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.search import hybrid_search

from .conftest import fake_settings


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("antworten")
    generate(tmp / "demo")
    s = fake_settings(fake_ai, tmp, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return s, ai, conn


def _ask(env, question, project=None):
    s, ai, conn = env
    return ask(conn, ai, str(s.db_path), question, project=project)


def test_snippet_is_the_quoted_passage_as_clean_text(env):
    """The source box shows the sentence the answer quotes ("8 Stunden"), not the chunk start."""
    r = _ask(env, "Wie lange muss das Notlicht funktionieren?", "BS-2024-0233")
    assert r.status == "answered" and "8 Stunden" in r.answer
    assert all("8 Stunden" in s["snippet"] or "8 h" in s["snippet"] for s in r.sources), r.sources
    r = _ask(env, "Wann ist die nächste Baubesprechung?", "BS-2024-0311")
    assert r.status == "answered" and "01.05.2025" in r.sources[0]["snippet"]
    for c in r.context + _ask(env, "Wer ist der Prüfingenieur im Hotel Seeblick?").context:
        assert len(c["snippet"]) <= 405 and "|" not in c["snippet"] and not c["snippet"].lstrip("… ").startswith("#")
        assert "Betreff:" not in c["snippet"] and "Von:" not in c["snippet"]


def test_answer_segments_belong_to_their_citation():
    assert answer_segments("A gilt [1]. B gilt [2, 3]. Und C [Quelle 1].", 3) == \
        {1: "A gilt Und C", 2: "B gilt", 3: "B gilt"}
    seg = answer_segments("Laut Baugenehmigung: Die Breite beträgt 1,0 m. [2][3]", 3)
    assert seg[2] == seg[3] and "1,0 m" in seg[2]


def test_user_texts_have_no_urls_or_setting_names(env, monkeypatch):
    s, ai, conn = env

    def timeout(*a, **k):
        raise AIServiceError("Zeitüberschreitung nach 120 s: http://172.31.250.1:18951/v1/chat/completions")

    monkeypatch.setattr(ai.llm, "chat", timeout)
    r = ask(conn, ai, str(s.db_path), "Wie hoch ist der Löschwasserbedarf?", project="BS-2023-0777")
    assert r.status == "llm_unavailable" and r.context
    assert r.notes == ["Das Sprachmodell hat nicht rechtzeitig geantwortet (Zeitüberschreitung nach 120 s)."]
    assert llm_error_note(AIServiceError("nicht erreichbar: http://x:1/v1 (refused)")) == \
        "Das Sprachmodell ist nicht erreichbar."
    assert "HTTP 503" in llm_error_note(AIServiceError("HTTP 503 von http://x/v1: busy"))
    weak = ask(conn, ai, str(s.db_path), "Wie viele Fahrradstellplätze gibt es?", project="BS-2024-0233",
               min_rerank=5.0)
    assert weak.notes and all("ASK_MIN_RERANK" not in n and "http" not in n for n in weak.notes)


def test_negative_fact_of_a_real_model_stays_an_answer(env, monkeypatch):
    """A1 regression: "… liegen keine Mängel mehr vor [1]." is the answer to "Ist der Mangel behoben?"."""
    s, ai, conn = env
    text = "Laut Abnahmeprotokoll vom 12.12.2024 liegen keine Mängel mehr vor [1]."
    monkeypatch.setattr(ai.llm, "chat", lambda *a, **k: ChatResult(text, text, {}, "m", 1.0, "stop"))
    r = ask(conn, ai, str(s.db_path), "Ist der Mangel an der Feststellanlage inzwischen behoben?",
            project="BS-2023-0412")
    assert r.status == "answered" and r.found is True and r.sources and "keine Mängel" in r.answer


@pytest.mark.parametrize("number,fact", [("BS-2023-0412", "12.12.2024"), ("BS-2024-0233", "Altbau"),
                                         ("BS-2025-0019", "Rahmenvertrag")])
def test_news_from_the_owner_reaches_the_context(env, number, fact):
    """"Was gibt es Neues vom Bauherrn?" shares no word with the owner's status mail – the newest
    sources of the project are put into the context anyway."""
    r = _ask(env, "Was gibt es Neues vom Bauherrn?", number)
    assert r.status == "answered" and fact in r.answer, r.answer
    r = _ask(env, "Was hat der Bauherr zuletzt geschrieben?", number)
    assert r.status == "answered" and "Teilnehmer" not in r.answer


def test_summary_question_gets_overview_passages(env):
    r = _ask(env, "Fasse das Projekt Kita Regenbogen zusammen.")
    assert r.status == "answered" and "Kindertagesstätte" in r.answer and len(r.sources) >= 2


def test_decision_of_the_reviewer_not_the_conditional_permit(env):
    r = _ask(env, "Hat der Prüfingenieur dem Abweichungsantrag zugestimmt?", "BS-2024-0233")
    assert r.status == "answered" and ("abgelehnt" in r.answer or "nicht zustimmen" in r.answer)
    assert "entschieden" not in r.answer
    r = _ask(env, "Hat der Prüfingenieur dem Abweichungsantrag zugestimmt?", "BS-2024-0137")
    assert r.status == "answered" and "stimme ich der Abweichung" in r.answer


@pytest.mark.parametrize("query,expected", [("BS-2024-0233", "BS-2024-0233"), ("63-0311/24", "63-0311/24"),
                                            ("BA-019/2025", "BA-019/2025"), ("2024-1187", "2024-1187")])
def test_reference_codes_find_their_documents_first(env, query, expected):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), query, k=10)
    assert res.hits and res.reranked
    literal = [expected in f"{h.title} {h.text}" for h in res.hits]
    assert literal[0] and literal == sorted(literal, reverse=True), [h.label for h in res.hits]
    assert res.hits[0].rerank and res.hits[0].rerank > 0.5  # the fake reranker scores literal codes high


def test_search_api_with_project_number(env):
    s, _, _ = env
    data = TestClient(create_app(s)).get("/api/search", params={"q": "BS-2023-0777"}).json()
    assert data["hits"] and all(h["project"] == "BS-2023-0777" for h in data["hits"][:5])
    assert data["hits"][0]["rerank"] >= 0.5
