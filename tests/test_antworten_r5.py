"""Answers over the demo data, round 5: a question's qualifier ("Wohnbereich C", "2. OG", "vor dem Umbau")
must be the answering sentence's; the transcribed Fluchtwegplan answers what it shows; "Was hat die
Prüfingenieurin bemängelt?" quotes the findings; summaries leave out the standard formulas every concept
ends with; header labels ("Betreff:") and reader-only readings ("geschrieben" ↔ "Verfasser:") do not leak
into the search; list lines are no section headings of a snippet."""
import sqlite3

import pytest

from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.demo import generate
from projektakte.fake_ai.core import (NOT_FOUND, active_synonyms, answer_from_sources, question_qualifiers,
                                      question_terms, read_units, rerank_score, _parse_header)
from projektakte.indexer import run_index
from projektakte.search import fts_query
from projektakte.textutil import builtin_synonyms, load_synonyms, plain_lines, relevant_passage, _passage_headings

from .conftest import fake_settings


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("antworten_r5")
    generate(tmp / "demo")
    s = fake_settings(fake_ai, tmp, DEMO_MODE=1, LLM_VISION=1)  # the plan is transcribed, as in the demo
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return s, ai, conn


def _ask(env, question, project=None):
    s, ai, conn = env
    return ask(conn, ai, str(s.db_path), question, project=project)


def _reader(question, *sources):
    raw = [(int(h[1:h.index("]")]), h.split("] ", 1)[1], t) for h, t in sources]
    return answer_from_sources(question, raw)


P = " – Projekt BS-2099-0009 Seniorenheim Lindenallee"


# --------------------------------------------------------------------------- 1. qualifiers
@pytest.mark.parametrize("question,project", [
    ("Wann wurde die Feststellanlage im Wohnbereich C des Pflegeheims abgenommen?", None),
    ("Wann wurde die Feststellanlage im Wohnbereich D des Pflegeheims Sonnenhang abgenommen?", None),
    ("Wann wurde die Feststellanlage im Wohnbereich C abgenommen?", "BS-2023-0412"),
    ("Wie lang ist der Stichflur im 2. Obergeschoss des Hotels Seeblick?", None),
    ("Wie breit ist der Fluchtbalkon im 2. Obergeschoss des Hotels Seeblick?", None),
    ("Welche Feuerwiderstandsklasse hat die Rauchschutztür im 3. Obergeschoss des Pflegeheims?", None),
    ("Wie viele Gastbetten hat das Hotel Seeblick vor dem Umbau?", None),
    ("Wie viele Einstellplätze hatte die Tiefgarage Marktplatz vor dem Neubau?", None),
])
def test_a_different_qualifier_is_no_answer(env, question, project):
    """All eight were answered (green) with the sentence about Wohnbereich B, the 3. OG, the 2. OG or the
    state after the conversion."""
    r = _ask(env, question, project)
    assert r.status == "not_found", r.answer


@pytest.mark.parametrize("question,fact", [
    ("Wann wurde die Feststellanlage im Wohnbereich B des Pflegeheims abgenommen?", "12.12.2024"),
    ("Wie lang ist der Stichflur im 3. Obergeschoss des Hotels Seeblick?", "17 m"),
    ("Welche Feuerwiderstandsklasse hat die Rauchschutztür im 2. Obergeschoss des Pflegeheims?", "T30-RS"),
    ("Wie viele Gastbetten hat das Hotel Seeblick nach dem Umbau?", "120"),
    ("Wie viele Gastbetten hat das Hotel Seeblick?", "120"),
    ("Wie wird der Mieterausbau als Großraumbüro im 4. OG bewertet?", "unkritisch"),
])
def test_the_matching_qualifier_still_answers(env, question, fact):
    r = _ask(env, question)
    assert r.status == "answered" and fact in r.answer, r.answer


def test_an_ordinal_floor_is_no_count_and_the_heading_names_the_floor(env):
    """"im 3. OG" is no number of Fluchtbalkone: the width comes from the draft whose heading names the
    3. Obergeschoss, not from the minutes that only mention the balcony."""
    r = _ask(env, "Wie breit ist der Fluchtbalkon im 3. Obergeschoss des Hotels Seeblick?")
    assert r.status == "answered" and "1,0 m" in r.answer and "geplant" not in r.answer, r.answer


SENIOR = ("[1] 02 Konzept/Brandschutzkonzept.docx (Stand 01.02.2025)" + P, (
    "# 3 Rettungswege\nDer Flur im Bauteil A ist 28 m lang. Im dritten Obergeschoss gibt es zwei Treppenräume.\n"
    "# 4 Bestand\nBisher hatte das Heim 60 Pflegeplätze. Nach dem Umbau bietet das Seniorenheim Platz für "
    "80 Bewohner auf 80 Pflegeplätzen."))


def test_qualifiers_in_synthetic_sources():
    assert "28 m" in _reader("Wie lang ist der Flur im Bauteil A?", SENIOR)
    assert _reader("Wie lang ist der Flur im Bauteil B?", SENIOR) == NOT_FOUND
    assert "zwei Treppenräume" in _reader("Wie viele Treppenräume gibt es im 3. OG?", SENIOR)
    assert _reader("Wie viele Treppenräume gibt es im zweiten Obergeschoss?", SENIOR) == NOT_FOUND
    before = _reader("Wie viele Pflegeplätze hatte das Seniorenheim vor dem Umbau?", SENIOR)
    assert "60" in before and "80" not in before, before
    assert "80 Pflegeplätzen" in _reader("Wie viele Pflegeplätze hat das Seniorenheim nach dem Umbau?", SENIOR)


def test_question_qualifiers():
    syn = active_synonyms("")

    def quals(q):
        return {(x.kind, x.value, x.head.term if x.head else None, x.group)
                for x in question_qualifiers(q, question_terms(q, syn), set())}

    assert quals("Wann wurde die Feststellanlage im Wohnbereich C abgenommen?") == {
        ("designator", "c", "wohnbereich", "")}
    assert quals("Wie lang ist der Stichflur im dritten Obergeschoss?") == {("floor", "3", None, "og")}
    assert quals("Wie viele Gastbetten hat das Hotel vor dem Umbau?") == {("time", "vor", "umbau", "")}
    assert quals("Was fordert Auflage 7 der Baugenehmigung?") == {("designator", "7", "auflag", "")}
    # measures, dates, years and the "vor" of an obligation are no qualifiers
    assert quals("Ist der Flur 38 m lang?") == set()
    assert quals("Was geschah im Februar 2025?") == set()
    assert quals("Wer hat die Fußball-WM 2014 gewonnen?") == set()
    assert quals("Was ist vor Ort zu prüfen?") == set()


# --------------------------------------------------------------------------- 2. the transcribed plan
@pytest.mark.parametrize("question,project,fact", [
    ("Wo ist der Sammelplatz der Grundschule Am Lindenhof?", None, "Sammelplatz Parkplatz Nord"),
    ("Wo ist der Sammelplatz?", "BS-2024-0137", "Sammelplatz Parkplatz Nord"),
    ("Wo ist laut Fluchtwegplan der Sammelplatz der Grundschule Am Lindenhof?", None, "Sammelplatz Parkplatz Nord"),
    ("Wo hängt im Erdgeschoss der Grundschule der Feuerlöscher?", None, "Feuerlöscher neben Treppenraum 1"),
    ("Wo ist der Notausgang der Grundschule Am Lindenhof?", None, "Notausgang Ost"),
])
def test_the_transcribed_escape_plan_answers_what_it_shows(env, question, project, fact):
    r = _ask(env, question, project)
    assert r.status == "answered" and fact in r.answer, r.answer
    assert "Fluchtwegplan EG" in r.sources[0]["label"], r.sources
    assert "Notausgang Ost, Sammelplatz" not in r.answer  # the item, not the whole legend


@pytest.mark.parametrize("question", [
    "Wo ist der Notausgang Nord der Grundschule Am Lindenhof?",  # "Notausgang Ost" + "Parkplatz Nord" ≠ it
    "Wo ist der Sammelplatz im Obergeschoss der Grundschule Am Lindenhof?",  # the plan shows the Erdgeschoss
    "Wo hängt der Feuerlöscher im Treppenraum 2 der Grundschule?",
    "Wie viele Feuerlöscher gibt es im Erdgeschoss der Grundschule?",  # "Treppenraum 1" counts nothing
    "Wo ist der Sammelplatz der Kita Regenbogen?",
])
def test_the_plan_does_not_answer_what_it_does_not_show(env, question):
    r = _ask(env, question)
    assert r.status == "not_found", r.answer


def test_a_legend_is_read_item_by_item():
    plan = ("[1] 05 Pläne/Fluchtwegplan OG.png, S. 1 (Stand 01.03.2025)" + P,
            "Transkription: Fluchtwegplan Obergeschoss. Notausgang West, Sammelplatz Wiese Süd, "
            "Wandhydrant neben Aufzug 2.")
    a = _reader("Wo ist der Sammelplatz?", plan)
    assert "Sammelplatz Wiese Süd" in a and "Notausgang" not in a and "Fluchtwegplan Obergeschoss" in a, a
    assert _reader("Wo ist der Notausgang Süd?", plan) == NOT_FOUND
    units = read_units(_parse_header(1, plan[0].split("] ", 1)[1], plan[1]))
    assert [u.text for u in units] == ["Notausgang West", "Sammelplatz Wiese Süd", "Wandhydrant neben Aufzug 2"]
    assert all(u.kind == "fact" and u.context == "Fluchtwegplan Obergeschoss" for u in units)
    # a list of people is one statement (no place, no number), a name list ending a mail stays its signature
    team = ("[2] 04 Protokolle/2025-03-01 Besprechungsprotokoll Nr. 1.txt (Stand 01.03.2025)" + P,
            "Anwesend: Ingrid Vogt, Paul Hinz, Lea Funke.\nDie Rampe wird verlängert.")
    units = read_units(_parse_header(2, team[0].split("] ", 1)[1], team[1]))
    assert units[0].text == "Ingrid Vogt, Paul Hinz, Lea Funke.", [u.text for u in units]


# --------------------------------------------------------------------------- 3. findings of a review
def test_what_the_reviewer_criticised_are_the_findings(env):
    r = _ask(env, "Was hat die Prüfingenieurin beim Hotel Seeblick bemängelt?")
    assert r.status == "answered" and "zulässige Maß von 15 m" in r.answer and "Steuermatrix" in r.answer, r.answer
    for empty in ("liegt vor", "bescheinigt", "Prüfingenieurbüro Wendehals"):
        assert empty not in r.answer, r.answer
    r = _ask(env, "Was hat die Prüfingenieurin im Prüfbericht Nr. 1 zum Hotel Seeblick festgestellt?")
    assert r.status == "answered" and "zulässige Maß von 15 m" in r.answer, r.answer
    assert "liegt vor" not in r.answer and "bescheinigt" not in r.answer, r.answer
    assert any("Prüfbericht Nr. 1" in s["label"] for s in r.sources), r.sources


def test_criticism_is_a_finding_that_something_is_wrong():
    report = ("[1] 03 Prüfung/Prüfbericht Nr. 2.md (Stand 09.09.2024)" + P, (
        "# Prüfbericht Nr. 2 zum Brandschutznachweis\n| Angabe | Inhalt |\n|---|---|\n"
        "| Prüfingenieur | Dipl.-Ing. Eva Muster, Prüfbüro Muster |\n## Feststellungen\n"
        "1. Die Zuluftnachströmung über die Rampe ist rechnerisch nachgewiesen.\n"
        "2. Die Ventilatoren der Entrauchung sind für 300 °C über 60 Minuten auszulegen.\n## Ergebnis\n"
        "Der Brandschutznachweis wird unter Beachtung der Feststellungen als vollständig und richtig bescheinigt."))
    cover = ("[2] Mail vom 11.09.2024 (Dipl.-Ing. Eva Muster): Prüfbericht Nr. 2" + P, (
        "Sehr geehrte Damen und Herren,\nmein Prüfbericht Nr. 2 vom 09.09.2024 liegt vor.\n"
        "Mit freundlichen Grüßen\nDipl.-Ing. Eva Muster\nPrüfingenieurin für Brandschutz"))
    criticised = _reader("Was hat die Prüfingenieurin bemängelt?", report, cover)
    assert "auszulegen" in criticised and "nachgewiesen" not in criticised, criticised
    assert "liegt vor" not in criticised and "bescheinigt" not in criticised and "Prüfbüro" not in criticised
    found = _reader("Was hat die Prüfingenieurin festgestellt?", report, cover)
    assert "nachgewiesen" in found and "auszulegen" in found and "bescheinigt" not in found, found
    assert _reader("Was hat die Prüfingenieurin bemängelt?", cover) == NOT_FOUND  # a cover mail finds nothing


# --------------------------------------------------------------------------- 4. summaries
@pytest.mark.parametrize("question,fact", [
    ("Fasse das Projekt Kita Regenbogen zusammen", "Kindertagesstätte"),
    ("Fasse das Projekt Hotel Seeblick zusammen", "120 Gastbetten"),
])
def test_summary_leaves_out_the_standard_conclusion(env, question, fact):
    r = _ask(env, question)
    assert r.status == "answered" and fact in r.answer, r.answer
    assert "keine Bedenken" not in r.answer and "mit dem Verfasser abzustimmen" not in r.answer, r.answer


def test_summary_without_an_assessment_of_its_own():
    head = "02 Konzept/Brandschutzkonzept.docx (Stand 01.02.2025)" + P  # one source per section, as indexed
    general = ("[1] " + head, "# 1 Allgemeines\nUmbau eines Seniorenheims mit 80 Pflegeplätzen.")
    closing = ("[2] " + head, "# 8 Zusammenfassung\nBei Umsetzung der beschriebenen Maßnahmen bestehen aus Sicht "
               "des Brandschutzes keine Bedenken gegen das Bauvorhaben. Änderungen der Planung sind mit dem Verfasser "
               "abzustimmen.")
    deviations = ("[3] " + head, "# 6 Abweichungen\nFür den Flur im Bauteil A (28 m) wird eine Abweichung beantragt.")
    a = _reader("Fasse das Projekt zusammen.", general, closing, deviations)
    assert "80 Pflegeplätzen" in a and "Abweichung beantragt" in a and "Bedenken" not in a, a
    assert "abzustimmen" not in _reader("Fasse das Projekt zusammen.", general, closing)
    # the formula still answers a question about it
    assert "keine Bedenken" in _reader("Bestehen aus Sicht des Brandschutzes Bedenken gegen das Bauvorhaben?",
                                       closing)


# --------------------------------------------------------------------------- 5. header labels, strong verbs
def test_a_mail_header_label_is_no_topic():
    syn = active_synonyms("")
    mail = "Betreff: Parkallee 50 – Wartung der Kaffeemaschine\nVon: Hausverwaltung\nDie Maschine wird gewartet."
    assert rerank_score("Wen betrifft die Räumungsübung?", mail, syn) == 0
    assert rerank_score("Welche Mängel betrifft die Nachprüfung?", mail, syn) == 0
    assert rerank_score("Wartung der Kaffeemaschine", mail, syn) > 0.5  # the subject itself still counts
    letter = ("[1] Schreiben Bauamt.pdf (Stand 01.03.2025)" + P,
              "Betreff: Wartung der Kaffeemaschine\nDie Maschine wird am Freitag gewartet.")
    assert _reader("Wen betrifft die Räumungsübung?", letter) == NOT_FOUND


def test_strong_verbs_are_matched_as_whole_verbs():
    syn = active_synonyms("")

    def forms(q, word):
        return {t.word: t for t in question_terms(q, syn)}[word].forms

    assert "vertret" in forms("Wer vertritt den Bauherrn?", "vertritt")
    assert "entsprech" in forms("Was entspricht der Norm?", "entspricht")
    assert "betreff" in forms("Welche Auflage betrifft die Feuerlöscher?", "betrifft")
    assert "vermess" not in forms("Wer vermisst die Unterlagen?", "vermisst")  # vermissen, not vermessen
    doc = "Auflage 6 (Feuerlöscher): Die Auflage betrifft alle Geschosse."
    assert rerank_score("Welche Auflage betrifft die Feuerlöscher?", doc, syn) > 0.5


# --------------------------------------------------------------------------- 6. reader readings stay in the reader
@pytest.mark.parametrize("query", ["Schreiben der Feuerwehr", "Feuerwehrplan erstellen", "Aufstellen der Rauchwarnmelder",
                                   "Aufstellfläche für die Feuerwehr", "Wann wurde das Gutachten erstellt?",
                                   "Wer hat das Konzept geschrieben?"])
def test_full_text_search_does_not_expand_to_the_author_label(query):
    assert "verfass" not in fts_query(query, expand=True, synonyms=load_synonyms(None))


def test_author_reading_only_for_who_wrote_it():
    syn = active_synonyms("")
    assert not any("verfass" in alt for _, targets in builtin_synonyms().one_way for alt in targets)

    def syns(q, word):
        return {t.word: t for t in question_terms(q, syn)}[word].syns

    assert ("verfass",) in syns("Wer hat das Konzept fürs Heim geschrieben?", "geschrieben")
    assert ("aufgestellt",) in syns("Wer hat das Protokoll erstellt?", "erstellt")  # the label, not "aufzustellen"
    assert ("verfass",) not in syns("Feuerwehrplan erstellen", "erstellen")
    assert ("verfass",) not in syns("Wann wurde das Gutachten erstellt?", "erstellt")
    header = "Brandschutzkonzept | Hotel Seeblick | Brandschutzkonzept\nVerfasser: Ingenieurbüro Feuerfest, Murat Yilmaz."
    assert rerank_score("Feuerwehrplan erstellen", header, syn) == 0


def test_demo_who_wrote_and_who_drew_up(env):
    r = _ask(env, "Wer hat das Besprechungsprotokoll Nr. 2 beim Hotel Seeblick erstellt?")
    assert r.status == "answered" and "Murat Yilmaz" in r.answer, r.answer


# --------------------------------------------------------------------------- 7. snippet headings
LIST_AFTER_COLON = ("Im Treppenhaus Nord sind zusätzlich Handfeuerlöscher vorzusehen, und zwar je Geschoss:\n"
                    "2 Pulverlöscher\nDie Löscher sind bis zum 30.06.2025 zu montieren.")
WRAPPED = ("Die Prüfung der Rauchwarnmelder erfolgt jährlich durch die Hausverwaltung in allen Wohnungen\n"
           "12 Monate nach Inbetriebnahme\nist die erste Prüfung fällig.")


def test_list_lines_are_no_section_headings():
    p = relevant_passage(LIST_AFTER_COLON, "Bis wann sind die Löscher im Treppenhaus Nord zu montieren?",
                         "Die Löscher sind bis zum 30.06.2025 zu montieren.")
    assert p.startswith("Im Treppenhaus Nord") and "30.06.2025" in p, p
    p = relevant_passage(WRAPPED, "Wann ist die erste Prüfung der Rauchwarnmelder fällig?",
                         "12 Monate nach Inbetriebnahme ist die erste Prüfung fällig.")
    assert p.startswith("Die Prüfung der Rauchwarnmelder") and "fällig" in p, p
    for text in (LIST_AFTER_COLON, WRAPPED, "Vorzuhalten sind:\n3 Wandhydranten\n2 Pulverlöscher"):
        assert _passage_headings(text, plain_lines(text)) == set(), text


def test_real_section_headings_are_still_found():
    text = ("Die Anlage ist geprüft.\n7 Organisatorischer Brandschutz\nNachts mindestens drei Pflegekräfte im Haus.\n"
            "3.2 Rettungswege\nDer Flur ist 1,20 m breit.\nTOP 2 – Räumungsübung\nDie Übung ist im Januar.")
    lines = plain_lines(text)
    assert {lines[i] for i in _passage_headings(text, lines)} == {
        "7 Organisatorischer Brandschutz", "3.2 Rettungswege", "TOP 2 – Räumungsübung"}
    md = "# 6 Abweichungen\nKeine.\n# 7 Organisatorischer Brandschutz\nNachts mindestens drei Pflegekräfte."
    assert {plain_lines(md)[i] for i in _passage_headings(md, plain_lines(md))} == {
        "6 Abweichungen", "7 Organisatorischer Brandschutz"}
    p = relevant_passage(text, "Wie viele Pflegekräfte sind nachts da?", "Nachts mindestens drei Pflegekräfte im Haus.")
    assert p.startswith("… 7 Organisatorischer Brandschutz\nNachts"), p
    p = relevant_passage(text, "Wie breit ist der Flur?", "Der Flur ist 1,20 m breit.")
    assert p.startswith("… 3.2 Rettungswege\nDer Flur") and "Pflegekräfte" not in p, p
