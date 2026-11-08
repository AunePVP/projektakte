"""Answers over the demo data, round 4: rephrased factual questions are answered by the fake reader
(word forms and synonyms – "geschrieben" ↔ Verfasser, "vertritt" ↔ "vertreten durch", contractions such
as "fürs", hyphen compounds "Funk-Rauchwarnmelder" ↔ "funkvernetzt", a place the sentence does not name
but another sentence of the project ties to its subject, the signature for "Wer ist … zuständig?") and
still refuse what the sources do not say; source snippets start at the heading of their section, not
in the foreign section before it."""
import sqlite3

import pytest

from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.demo import generate
from projektakte.fake_ai.core import NOT_FOUND, active_synonyms, answer_from_sources, question_terms
from projektakte.indexer import run_index
from projektakte.textutil import relevant_passage

from .conftest import fake_settings


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("antworten_r4")
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


@pytest.mark.parametrize("question,fact,label", [
    ("Wer hat das Brandschutzkonzept fürs Pflegeheim Sonnenhang geschrieben?", "Lea Funke", "Brandschutzkonzept"),
    ("Wer hat das Brandschutzkonzept fürs Pflegeheim Sonnenhang verfasst?", "Lea Funke", "Brandschutzkonzept"),
    ("Welche Türöffnungskraft wird im Sicherheitstreppenraum der Hafenkante eingehalten?", "100 N", "Prüfbericht Nr. 2"),
    ("Bis wann wird der Hersteller der Funk-Rauchwarnmelder für die Kita festgelegt?", "August",
     "Besprechungsprotokoll Nr. 2"),
    ("Wer vertritt den Bauherrn beim Hotel Seeblick?", "Konstantin Rehberg", "Baugenehmigung"),
    ("Wer ist bei der Feuerwehr Musterstadt für den vorbeugenden Brandschutz zuständig?", "Uwe Kessler",
     "Uwe Kessler"),
])
def test_rephrased_questions_are_answered(env, question, fact, label):
    """All five ended with "nichts gefunden" while the evidence was listed as [1] under "Mögliche
    Fundstellen"."""
    r = _ask(env, question)
    assert r.status == "answered" and fact in r.answer, r.answer
    assert any(label in s["label"] for s in r.sources), r.sources


@pytest.mark.parametrize("question", [
    "Welche Türöffnungskraft wird im Keller der Hafenkante eingehalten?",  # no source puts it in the Keller
    "Wer ist bei der Feuerwehr Musterstadt für die Abnahme zuständig?",  # the signature says nothing about it
    "Wer vertritt den Prüfingenieur beim Hotel Seeblick?",  # "vertreten durch" belongs to the Bauherr
    "Bis wann wird der Hersteller der Funk-Rauchwarnmelder für das Pflegeheim festgelegt?",  # another project
])
def test_the_same_phrasings_still_fail_closed(env, question):
    r = _ask(env, question)
    assert r.status == "not_found", r.answer


def test_question_terms_of_typed_questions():
    syn = active_synonyms()
    terms = {t.word: t for t in question_terms("Wer hat das Konzept fürs Heim geschrieben?", syn)}
    assert "furs" not in terms and ("verfass",) in terms["geschrieben"].syns
    funk = {t.word: t for t in question_terms("Welche Funk-Rauchwarnmelder gibt es?", syn)}["funk"]
    assert "funk" in funk.forms  # the first part of a hyphen compound starts compounds: "funkvernetzt"
    assert "vertret" in {t.word: t for t in question_terms("Wer vertritt den Bauherrn?", syn)}["vertritt"].forms


P = " – Projekt BS-2099-0007 Bürohaus Kanalufer"
REPORT = ("[1] 03 Prüfung/Prüfbericht Nr. 1.md (Stand 09.09.2024)" + P, (
    "## Feststellungen\n1. Die Auslegung der Druckbelüftungsanlage ist nachvollziehbar; die Türöffnungskraft "
    "von 100 N wird eingehalten."))
CONCEPT = ("[2] 02 Konzept/Brandschutzkonzept.docx (Stand 19.03.2024)" + P, (
    "# 4 Anlagentechnischer Brandschutz\nDie Druckbelüftungsanlage hält im Sicherheitstreppenraum einen Überdruck "
    "von 15 bis 50 Pa.\n# 5 Tiefgarage\nDie Tiefgarage hat 40 Einstellplätze."))


def _reader(question, *sources):
    raw = [(int(h[1:h.index("]")]), h.split("] ", 1)[1], t) for h, t in sources]
    return answer_from_sources(question, raw)


def test_a_place_counts_when_another_source_puts_the_subject_there():
    q = "Welche Türöffnungskraft wird im Sicherheitstreppenraum eingehalten?"
    assert "100 N" in _reader(q, REPORT, CONCEPT)
    assert _reader(q, REPORT) == NOT_FOUND  # nothing ties the Druckbelüftungsanlage to the Sicherheitstreppenraum
    assert _reader("Welche Türöffnungskraft wird in der Tiefgarage eingehalten?", REPORT, CONCEPT) == NOT_FOUND


def test_signature_answers_who_someone_is_but_states_nothing():
    mail = ("[1] Mail vom 15.05.2024 (Brandoberinspektor Uwe Kessler): Stellungnahme" + P, (
        "Sehr geehrte Damen und Herren,\nDas Feuerwehrschlüsseldepot ist am Haupteingang vorzusehen.\n"
        "Mit freundlichen Grüßen\nBrandoberinspektor Uwe Kessler\nFeuerwehr Musterstadt, Vorbeugender Brandschutz"))
    assert "Uwe Kessler" in _reader("Wer ist bei der Feuerwehr für den vorbeugenden Brandschutz zuständig?", mail)
    said = _reader("Was hat die Feuerwehr zum Feuerwehrschlüsseldepot gesagt?", mail)
    assert "Haupteingang" in said and "Vorbeugender" not in said


SECTIONS = ("# 6 Abweichungen\nKeine Abweichungen beantragt.\n# 7 Organisatorischer Brandschutz\n"
            "Nachts mindestens drei Pflegekräfte im Haus; Evakuierungshilfen (Rettungstücher) in jedem Zimmer.")


def test_passage_starts_at_the_heading_of_its_section():
    p = relevant_passage(SECTIONS, "Wie viele Pflegekräfte müssen nachts im Pflegeheim Sonnenhang da sein?",
                         "Laut Brandschutzkonzept: Nachts mindestens drei Pflegekräfte im Haus; Evakuierungshilfen "
                         "(Rettungstücher) in jedem Zimmer. [1]", 400)
    assert p == ("… 7 Organisatorischer Brandschutz\nNachts mindestens drei Pflegekräfte im Haus; Evakuierungshilfen "
                 "(Rettungstücher) in jedem Zimmer."), p
    minutes = ("Besprechungsprotokoll Nr. 7\nTOP 1 – Feststellanlage\nDer Türhersteller führt die Abnahme am "
               "12.12.2024 durch.\nTOP 2 – Räumungsübung\nDie nächste Räumungsübung findet im Januar statt.")
    p = relevant_passage(minutes, "Wann ist die nächste Räumungsübung?")
    assert p.startswith("… TOP 2 – Räumungsübung\nDie nächste") and "Türhersteller" not in p, p
    # a heading too far before the sentence is left out (at most PASSAGE_LEAD of text before it), and the
    # passage never reaches back into the section before it
    long_section = ("# 1 Allgemeines\nDas Gebäude hat drei Geschosse.\n# 2 Rettungswege\n"
                    + "Der Flur ist frei zu halten. " * 8 + "\nDie Außentreppe ist 1,20 m breit.")
    p = relevant_passage(long_section, "Wie breit ist die Außentreppe?", "Die Außentreppe ist 1,20 m breit.")
    assert "1,20 m" in p and "Allgemeines" not in p and "drei Geschosse" not in p and len(p) <= 402, p
    short = relevant_passage("# 2 Rettungswege\nDer Flur ist frei zu halten.\nDie Außentreppe ist 1,20 m breit.",
                             "Wie breit ist die Außentreppe?")
    assert short.startswith("2 Rettungswege\nDer Flur"), short  # the whole text: no "… "


def test_demo_snippet_starts_at_the_section(env):
    r = _ask(env, "Wie viele Pflegekräfte müssen nachts im Pflegeheim Sonnenhang da sein?")
    assert r.status == "answered" and "drei Pflegekräfte" in r.answer, r.answer
    snippet = r.sources[0]["snippet"]
    assert "7 Organisatorischer Brandschutz\nNachts mindestens drei" in snippet and "Abweichungen" not in snippet, \
        snippet
