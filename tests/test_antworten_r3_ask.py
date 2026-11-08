"""Answers over the demo data, round 3: the verifier's confident wrong answers are refused, the
signer and the LV attachment are found, Feuerwehr snippets show the statement (not the signature),
the latest state comes from the asked project, overview questions in free phrasings, partial refusals
with a cited fact stay answers, and evaluate refuses an empty selection."""
import json
import sqlite3

import pytest

from projektakte.ai.llm import ChatResult
from projektakte.ai.services import AIServices
from projektakte.ask import ask, is_not_found
from projektakte.demo import generate
from projektakte.evaluate import main, print_summary, summarize
from projektakte.indexer import run_index
from projektakte.search import hybrid_search
from projektakte.textutil import relevant_passage

from .conftest import fake_settings


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("antworten_r3")
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


@pytest.mark.parametrize("question,project", [
    ("Wann wurde die Brandmeldeanlage im Pflegeheim Sonnenhang abgenommen?", None),
    ("Wann wurde die BMA im Pflegeheim abgenommen?", None),
    ("Wann wurde die Brandmeldeanlage abgenommen?", "BS-2023-0412"),
    ("Wann wurden die Feuerwehrpläne für das Pflegeheim übergeben?", None),
    ("Wie breit ist die Balkontreppe?", None),
    ("Wie breit ist die Balkontreppe?", "BS-2025-0019"),
    ("Wie lang ist die Außentreppe der Grundschule?", None),
    ("Wie hoch ist die Außentreppe der Grundschule?", None),
    ("Wie breit ist der Stichflur im Hotel Seeblick?", None),
    ("Wie tief ist der Löschwasserteich?", None),
    ("Wie lang ist der Flur im Pflegeheim?", None),
    ("Wie hoch ist die Einfahrt der Tiefgarage des Bürogebäudes Hafenkante?", None),
    ("Welchen Luftwechsel braucht die Tiefgarage vom Bürogebäude Hafenkante?", None),
    ("Welcher Prüfsachverständige nimmt die BMA der Grundschule ab?", None),
])
def test_verifier_wrong_answers_are_refused(env, question, project):
    """Mixed-up dimensions, an obligation or a plan for a past event, another project than the named
    one, a date for "who" – all were answered green with a source in round 2."""
    r = _ask(env, question, project)
    assert r.status == "not_found", r.answer


def test_right_dimension_and_event_still_answered(env):
    assert "38 m" in _ask(env, "Wie lang ist der Flur im Obergeschoss der Grundschule?").answer
    assert "1,20 m" in _ask(env, "Wie breit ist die Außentreppe der Grundschule?").answer
    assert "2,10 m" in _ask(env, "Wie hoch ist die Einfahrt der Tiefgarage Marktplatz?").answer
    assert "68 m" in _ask(env, "Wie hoch ist das Wohnhochhaus?").answer
    assert "30.11.2022" in _ask(env, "Wann wurde die Baugenehmigung für die Tiefgarage Marktplatz erteilt?").answer
    assert "12.12.2024" in _ask(env, "Wann wurde die Feststellanlage im Pflegeheim abgenommen?").answer
    r = _ask(env, "Reicht ein Vorraum von 5,5 m² für den Feuerwehraufzug im Wohnhochhaus?")
    assert r.status == "answered" and "6 m²" in r.answer and "5,5" not in r.answer, r.answer


def test_signer_and_lv_attachment_are_found(env):
    r = _ask(env, "Wer hat die Stellungnahme der Feuerwehr zum Hotel Seeblick unterschrieben?")
    assert r.status == "answered" and "Holger Lamm" in r.answer, r.answer
    r = _ask(env, "Wie viele Rauchwarnmelder sind für die Kita Regenbogen ausgeschrieben?")
    assert r.status == "answered" and "24" in r.answer, r.answer
    assert any("LV_Rauchwarnmelder" in s["label"] for s in r.sources)


@pytest.mark.parametrize("number", ["BS-2023-0777", "BS-2023-0412", "BS-2024-0058", "BS-2024-0233",
                                    "BS-2022-0901", "BS-2025-0019"])
def test_fire_service_snippets_show_the_statement_not_the_signature(env, number):
    """6 of 26 cited Feuerwehr sources showed "… Brandoberinspektor Uwe Kessler / Feuerwehr …"."""
    r = _ask(env, "Was hat die Feuerwehr gesagt?", number)
    assert r.status == "answered" and r.sources
    for src in r.sources:
        snippet = src["snippet"]
        last = snippet.rstrip().split("\n")[-1]
        assert not last.startswith(("Brand", "Stadtbrand", "Feuerwehr ", "Freiwillige")), snippet
        assert "Betreff:" not in snippet, snippet


def test_latest_state_comes_from_the_asked_project(env):
    r = _ask(env, "Was ist der Stand bei der Tiefgarage Marktplatz?")
    assert r.status == "answered" and "September" in r.answer, r.answer
    assert all(c["project"] in ("BS-2022-0901", None) for c in r.context[-2:])
    r = _ask(env, "Bitte kurze Rückmeldung zum Stand", "BS-2023-0412")  # "Rückmeldung" is no topic here
    assert r.status == "answered" and "Feststellanlage" in r.answer, r.answer


def test_party_questions_get_the_partys_mails(env):
    r = _ask(env, "Was wollte der Bauherr vom Wohnhochhaus wissen?")
    assert r.status == "answered" and "Aufstellfläche" in r.answer and "vertreten durch" not in r.answer, r.answer
    r = _ask(env, "Was hat die Architektin zuletzt gefragt?", "BS-2024-0311")
    assert r.status == "answered" and "?" in r.answer and "5,5 m²" in r.answer, r.answer


def test_overview_phrasings(env):
    for q, project, fact in (("Gib mir einen Überblick über die Tiefgarage Marktplatz.", None, "Großgarage"),
                             ("Was ist das für ein Projekt?", "BS-2025-0019", "Kindertagesstätte"),
                             ("Was wird hier gebaut?", "BS-2023-0777", "Produktionshalle"),
                             ("Erzähl mir was über das Projekt", "BS-2022-0901", "Großgarage"),
                             ("Kurze Zusammenfassung bitte", "BS-2024-0233", "Hotel")):
        r = _ask(env, q, project)
        assert r.status == "answered" and fact in r.answer and "Rückmeldung" not in r.answer, (q, r.answer)
    r = _ask(env, "Fasse bitte kurz zusammen, was die Feuerwehr will.", "BS-2024-0137")
    assert r.status == "answered" and "Stellungnahme Feuerwehr" in r.answer, r.answer  # the topic, not the project


def test_enumeration_by_person_quotes_no_courtesy(env):
    r = _ask(env, "An welchen Projekten arbeitet Murat Yilmaz?")
    assert r.status == "answered" and "Danke für die Nachfrage" not in r.answer and "melde mich" not in r.answer


def test_nonsense_with_a_digit_finds_nothing_relevant(env):
    s, ai, conn = env
    res = hybrid_search(conn, ai, str(s.db_path), "<script>alert(1)</script>", k=20)
    assert res.hits and all((h.rerank or 0) == 0 for h in res.hits)


@pytest.mark.parametrize("answer", [
    "Die Abnahme ist für März vorgesehen [2]; ein genauer Tag ist in den Quellen nicht genannt.",
    "Die Quellen nennen kein genaues Datum; laut Besprechungsprotokoll ist die Abnahme für März vorgesehen [2].",
    "Ein konkretes Datum nennen die Unterlagen nicht – die Abnahme ist laut [2] für März vorgesehen.",
    "Die Unterlagen enthalten keinen Hinweis auf einen offenen Mangel; das Abnahmeprotokoll vom 12.12.2024 meldet "
    "„Keine Mängel“ [1].",
])
def test_partial_refusal_with_a_cited_fact_is_an_answer(answer):
    assert not is_not_found(answer, 3)


@pytest.mark.parametrize("answer", ["Die Quellen enthalten dazu keine Angaben [1].",
                                    "Die Quellen nennen keinen Termin; die Abnahme ist offen.",
                                    "Dazu steht nichts in den Unterlagen – bitte beim Bauherrn nachfragen."])
def test_refusals_stay_refusals(answer):
    assert is_not_found(answer, 3)


def test_partial_refusal_through_ask_keeps_the_citation(env, monkeypatch):
    s, ai, conn = env
    text = "Die Abnahme ist für März vorgesehen [1]; ein genauer Tag ist in den Quellen nicht genannt."
    monkeypatch.setattr(ai.llm, "chat", lambda *a, **k: ChatResult(text, text, {}, "m", 1.0, "stop"))
    r = ask(conn, ai, str(s.db_path), "Wann wird die Druckbelüftungsanlage abgenommen?", project="BS-2024-0058")
    assert r.status == "answered" and r.sources and "März" in r.answer


MAIL = ("Betreff: Stellungnahme Brandschutzdienststelle – BS-2023-0777 Industriehalle Nordring\n"
        "Von: Brandoberinspektor Uwe Kessler <vb@feuerwehr.example>\nDatum: 21.09.2023 10:00\n"
        "Sehr geehrte Damen und Herren,\nzum Bauvorhaben Industriehalle Nordring nehmen wir wie folgt Stellung: Der "
        "Löschwasserteich ist mit einer Saugstelle nach DIN 14210 zu versehen. Die Zufahrt ist freizuhalten.\n"
        "Mit freundlichen Grüßen\nBrandoberinspektor Uwe Kessler\nFeuerwehr Musterstadt, Vorbeugender Brandschutz")


def test_passage_skips_the_lead_and_the_signature():
    p = relevant_passage(MAIL, "Was hat die Feuerwehr gesagt?", "Laut Mail von Brandoberinspektor Uwe Kessler vom "
                         "21.09.2023: Der Löschwasserteich ist mit einer Saugstelle nach DIN 14210 zu versehen")
    assert "Saugstelle" in p and "Uwe Kessler" not in p and "Musterstadt" not in p and "Betreff" not in p
    p = relevant_passage(MAIL, "Wer hat die Stellungnahme unterschrieben?",
                         "Laut Mail vom 21.09.2023, Unterschrift: Brandoberinspektor Uwe Kessler, Feuerwehr Musterstadt")
    assert "Uwe Kessler" in p  # the signer question quotes the signature
    no_closing = "Betreff: Handlauf\nVon: Jonas Feld <jf@x.example>\nHallo Murat,\nDer Handlauf wird in 60 cm Höhe montiert.\nJonas Feld"
    p = relevant_passage(no_closing, "Wie hoch wird der Handlauf montiert?", "Der Handlauf wird in 60 cm Höhe montiert.")
    assert "60 cm" in p and "Jonas Feld" not in p
    same_line = ("Guten Tag,\ndie Mitglieder möchten die Bäume vor dem Hochhaus behalten. Können wir die Fläche "
                 "verschieben? Benedikt Sauer, Wohnbau Stadtpark eG")
    p = relevant_passage(same_line, "Was gibt es Neues?", "Die Mitglieder möchten die Bäume vor dem Hochhaus behalten.")
    assert "Bäume" in p and "Wohnbau" not in p


def test_passage_starts_with_the_first_quoted_sentence():
    text = ("Auflagen zum Brandschutz\nAuflage 7 (Rettungswege): Für die Unterrichtsräume im Obergeschoss ist ein "
            "zweiter baulicher Rettungsweg über eine Außentreppe an der Ostseite herzustellen. Die Außentreppe ist aus "
            "nichtbrennbaren Baustoffen auszuführen.\nAuflage 8 (Feuerlöscher): Feuerlöscher sind bereitzustellen.")
    answer = ("Laut Baugenehmigung (S. 2), Auflage 7 (Rettungswege): Für die Unterrichtsräume im Obergeschoss ist ein "
              "zweiter baulicher Rettungsweg über eine Außentreppe an der Ostseite herzustellen. Die Außentreppe ist "
              "aus nichtbrennbaren Baustoffen auszuführen.")
    p = relevant_passage(text, "Was fordert Auflage 7?", answer)
    assert "Für die Unterrichtsräume" in p, p


def test_evaluate_refuses_an_empty_selection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert main(["--fake", "--only", "gibtsnicht"]) == 2
    assert "Keine Fragen ausgewählt" in capsys.readouterr().err
    empty = tmp_path / "leer.json"
    empty.write_text("[]", encoding="utf-8")
    assert main(["--fake", "--eval", str(empty)]) == 2
    assert main(["--fake", "--category", "gibtsnicht"]) == 2
    assert not (tmp_path / "data" / "eval" / "projektakte.sqlite3").exists()  # nothing indexed for nothing


def test_summary_marks_a_target_without_questions_as_not_checked(capsys):
    from projektakte.evaluate import Record
    rec = Record("n", "negative", "?", None, True, None, "not_found", True, [], "", [], None, 0.1, 1.0, 2.0, 1.0)
    s = summarize([rec])
    print_summary(s)
    out = capsys.readouterr().out
    assert "nicht geprüft" in out and s["correct"] is None and s["targets_met"]
    json.dumps(s)  # stays serialisable for --json
