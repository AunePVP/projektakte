"""Answers over the demo data, round 7 – the last wrong answers of the fake reader, fail-closed: a label such as
"Nächste Besprechung:" names another meeting than the protocol's number; the floor noun of "im 3. OG" belongs to the
qualifier, so "in allen Geschossen" no longer answers anything about that floor; an author field answers only for
the document it stands in; a defect word is read in its clause ("Es fehlen keine Unterlagen", "darf nicht ohne
Abnahme …" are no defects); a defect label names its own sentence only; "Obergeschoss 2 zusätzliche Feuerlöscher" is
a count; "vor der Eröffnung" is no "bei der Eröffnung" in a question about a state; "Raum 2-14" is one room; a norm's
part is part of its designator; what is still to be done ("Die Abnahme ist nachzuholen") was not found at it; a party
is not named by a compound ("Feuerwehraufzug"); a date in a question must be the sentence's (or its source's); a
proposal is no event that took place; "Was wünscht/fordert X?" needs a wish or demand of X."""
import sqlite3

import pytest

from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.demo import generate
from projektakte.fake_ai.core import (NOT_FOUND, _parse_header, active_synonyms, analyse_question, answer_from_sources,
                                      question_qualifiers, question_terms, read_units)
from projektakte.indexer import run_index

from .conftest import fake_settings


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("antworten_r7")
    generate(tmp / "demo")
    s = fake_settings(fake_ai, tmp, DEMO_MODE=1, LLM_VISION=1)
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


def _units(source):
    header, text = source
    return read_units(_parse_header(int(header[1:header.index("]")]), header.split("] ", 1)[1], text))


def _quals(question):
    syn = active_synonyms("")
    return {(x.kind, x.value, x.head.term if x.head else None)
            for x in question_qualifiers(question, question_terms(question, syn), set())}


P = " – Projekt BS-2099-0009 Seniorenheim Lindenallee"
DOC = "[1] 02 Konzept/Brandschutzkonzept.docx (Stand 01.02.2024)" + P
PB = "[2] 03 Prüfung/Prüfbericht Nr. 1.md (Stand 01.03.2024)" + P
FW = "[3] Mail vom 27.02.2024 (Uwe Kessler, Feuerwehr): Stellungnahme" + P
PROT = "[3] 04 Protokolle/2024-03-01 Besprechungsprotokoll Nr. 2.txt" + P


# --------------------------------------------------------------------------- 1. another one of a kind
@pytest.mark.parametrize("question", [
    "Wann war die zweite Besprechung beim Hotel Seeblick?",  # was "Nächste Besprechung: 04.12.2024"
    "Wann fand die zweite Besprechung beim Hotel Seeblick statt?",
    "Wo fand die zweite Besprechung beim Hotel Seeblick statt?",
    "Wann war die zweite Besprechung bei der Kita?",
    "Wann war die fünfte Besprechung zur Tiefgarage?",
    "Wann war die siebte Besprechung beim Pflegeheim?",
])
def test_the_next_meeting_is_not_the_numbered_one(env, question):
    r = _ask(env, question)
    assert r.status == "not_found", r.answer


def test_the_next_meeting_and_the_numbered_one_in_demo(env):
    r = _ask(env, "Wann ist die nächste Besprechung bei der Kita?")
    assert r.status == "answered" and "29.07.2025" in r.answer, r.answer
    r = _ask(env, "An welchem Datum war die Besprechung Nr. 5 zur Tiefgarage?")
    assert r.status == "answered" and "22.06.2023" in r.answer and "20.07.2023" not in r.answer, r.answer


def test_the_next_meeting_in_synthetic_sources():
    protocol = (PROT, "Besprechungsprotokoll Nr. 2\nTOP 1 Rettungswege: Die Außentreppe wird verbreitert.\n"
                      "Nächste Besprechung: 04.12.2024\nAufgestellt: Murat Yilmaz")
    assert _reader("Wann war die zweite Besprechung?", protocol) == NOT_FOUND
    assert _reader("Wann war die dritte Besprechung?", protocol) == NOT_FOUND
    assert "04.12.2024" in _reader("Wann ist die nächste Besprechung?", protocol)


# --------------------------------------------------------------------------- 2. floors
@pytest.mark.parametrize("question", [
    "Was wünscht der Mieter im dritten Obergeschoss der Hafenkante?",  # was "… aller Geschosse darzustellen."
    "Was wünscht der Mieter im 2. Obergeschoss der Hafenkante?",
    "Was plant der Mieter im fünften Obergeschoss der Hafenkante?",
])
def test_all_floors_do_not_answer_what_another_floor_wants(env, question):
    r = _ask(env, question)
    assert r.status == "not_found", r.answer


def test_a_rule_for_all_floors_still_answers_for_one(env):
    r = _ask(env, "Wie groß muss der Vorraum des Feuerwehraufzugs im 25. Obergeschoss sein?")
    assert r.status == "answered" and "6 m²" in r.answer, r.answer
    r = _ask(env, "Was wünscht der Mieter im 4. OG?", "BS-2024-0058")
    assert r.status == "answered" and "Großraumbüro" in r.answer, r.answer


def test_floors_in_synthetic_sources():
    shafts = (PB, "## Feststellungen\nDie Abströmschächte sind in den Grundrissen aller Geschosse darzustellen.")
    assert _reader("Was wünscht der Mieter im 3. OG?", shafts) == NOT_FOUND
    # a count after a floor noun is no floor number
    extinguishers = (DOC, "# 7 Löscher\nIm Erdgeschoss sind 4 und im Obergeschoss 2 zusätzliche Feuerlöscher "
                          "vorzusehen.")
    assert _reader("Wie viele Feuerlöscher sind im 2. OG vorzusehen?", extinguishers) == NOT_FOUND
    exits = (DOC, "# 3 Rettungswege\nIm Obergeschoss 2 weitere Notausgänge sind über die Außentreppe erreichbar.")
    assert _reader("Wie viele Notausgänge gibt es im 2. Obergeschoss?", exits) == NOT_FOUND
    # a compound is no floor: "Geschossanzahl 5" is not the 5. Geschoss
    count = (DOC, "# 1 Gebäude\nGeschossanzahl 5, Gebäudehöhe 15 m.")
    assert _reader("Wie hoch ist das 5. Geschoss?", count) == NOT_FOUND
    # the floor after its noun still counts
    assert _reader("Welche Feuerwiderstandsklasse haben die Rauchschutztüren im 3. OG?",
                   (DOC, "# 4 Türen\nDie Rauchschutztüren im OG 3 sind als T30-RS ausgeführt.")) != NOT_FOUND


def test_a_tenant_is_no_rent():
    q = analyse_question("Was wünscht der Mieter im dritten Obergeschoss?", [])
    assert "miet" in {t.term for t in q.terms} and not q.attrs
    assert "miet" in {t.term for t in analyse_question("Wie hoch ist die Miete?", []).attrs}


def test_the_floor_noun_belongs_to_the_qualifier():
    q = analyse_question("Wie lang ist der Stichflur im dritten Obergeschoss?", [])
    assert [t.term for t in q.terms] == ["stichflur"] and [x.value for x in q.qualifiers] == ["3"]


def test_a_floor_question_takes_no_shortcut():
    old = (DOC, "# 3 Rettungswege\nIm 3. OG wird ein Fluchtbalkon angebaut.")
    news = ("[2] Mail vom 05.03.2025 (Erika Muster): Rechnung" + P, "Hallo,\ndie Rechnung folgt separat.\nGruß\nErika")
    assert "Rechnung" not in _reader("Was ist der aktuelle Stand im 3. OG?", old, news)


# --------------------------------------------------------------------------- 3. authors
@pytest.mark.parametrize("question,project", [
    ("Wer hat das Abnahmeprotokoll erstellt?", "BS-2023-0412"),  # was the Besprechungsprotokoll's "Aufgestellt: …"
    ("Wer hat die Rauchmelder aufgestellt?", "BS-2025-0019"),
    ("Wer hat das Abnahmeprotokoll der Druckbelüftungsanlage erstellt?", None),
])
def test_an_author_field_answers_for_its_own_document_only(env, question, project):
    r = _ask(env, question, project)
    assert r.status == "not_found", r.answer


def test_authors_in_synthetic_sources():
    protocol = (PROT, "Besprechungsprotokoll Nr. 2\nTOP 1: Das Abnahmeprotokoll liegt vor.\nAufgestellt: Murat Yilmaz")
    assert _reader("Wer hat das Abnahmeprotokoll erstellt?", protocol) == NOT_FOUND
    assert "Murat Yilmaz" in _reader("Wer hat das Protokoll erstellt?", protocol)
    smoke = (PROT, "Besprechungsprotokoll Nr. 2\nTOP 2 – Rauchwarnmelder\nDie Melder werden im Januar montiert.\n"
                   "Aufgestellt: Murat Yilmaz")
    assert _reader("Wer hat die Rauchwarnmelder aufgestellt?", smoke) == NOT_FOUND


# --------------------------------------------------------------------------- 4. criticism in its clause
@pytest.mark.parametrize("text", [
    "Es fehlen keine Unterlagen. Die Zufahrt ist ausreichend.",
    "Die Brandmeldeanlage ist nicht nur ausreichend, sondern vorbildlich geplant.",
    "Die Anlage darf nicht ohne Abnahme in Betrieb genommen werden.",
    "Ohne Prüfung durch einen Sachverständigen darf die Anlage nicht in Betrieb gehen.",
    "Die Feuerwehrzufahrt wird ohne Genehmigung der Feuerwehr nicht verändert.",
    "Die Länge des Flurs überschreitet das zulässige Maß nicht.",
    "Sollte die Zufahrt nicht ausreichen, ist eine zweite vorzusehen.",
    "Etwaige Mängel sind vor der Abnahme zu beheben.",
    "Mängel wurden nicht festgestellt.",
])
def test_no_defect_in_a_partys_own_words(text):
    assert _reader("Was hat die Feuerwehr bemängelt?", (FW, text)) == NOT_FOUND


@pytest.mark.parametrize("text,fact", [
    ("Die Zufahrt ist nicht ausreichend breit.", "nicht ausreichend breit"),
    ("Die Feststellanlage wurde ohne Abnahmeprüfung in Betrieb genommen.", "ohne Abnahmeprüfung"),
    ("Es fehlt nicht nur die Zufahrt, sondern auch die Aufstellfläche.", "fehlt nicht nur"),
    ("Die Anlage darf nicht in Betrieb gehen, da die Laufkarten fehlen.", "Laufkarten fehlen"),
])
def test_a_defect_in_a_partys_own_words_is_still_one(text, fact):
    assert fact in _reader("Was hat die Feuerwehr bemängelt?", (FW, text))


def test_a_defect_label_names_its_own_sentence_only():
    mail = (FW, "Vielen Dank für die Begehung. Mangel: Die Tür im Treppenraum schließt nicht. Die Brandmeldeanlage "
                "funktioniert einwandfrei.")
    labels = {u.text: u.label for u in _units(mail)}
    assert labels["Die Tür im Treppenraum schließt nicht."] == "Mangel"
    assert labels["Die Brandmeldeanlage funktioniert einwandfrei."] is None
    assert _reader("Was hat die Feuerwehr an der Brandmeldeanlage bemängelt?", mail) == NOT_FOUND
    a = _reader("Welche Mängel hat die Feuerwehr festgestellt?", mail)
    assert "schließt nicht" in a and "einwandfrei" not in a, a
    # a label starts its sentence: "Vielen Dank. Auflage: …" is a sentence and a labelled one
    letter = ("[2] Mail vom 05.03.2024 (Erika Muster): Genehmigung" + P,
              "Die Baugenehmigung liegt vor. Auflage: Die Brandmeldeanlage ist aufzuschalten.")
    assert {u.text: u.label for u in _units(letter)}["Die Brandmeldeanlage ist aufzuschalten."] == "Auflage"


def test_the_reviewers_defects_stay_whole(env):
    r = _ask(env, "Was hat die Prüfingenieurin beim Projekt Pflegeheim Sonnenhang bemängelt?")
    assert r.status == "answered" and "ohne Abnahmeprüfung" in r.answer and "nachzuholen" in r.answer, r.answer


# --------------------------------------------------------------------------- 5. what was found at an occasion
@pytest.mark.parametrize("question", [
    "Welche Mängel gab es bei der Abnahme?",  # was "Mangel: Die Abnahme durch eine Fachkraft ist nachzuholen."
    "Welche Mängel wurden bei der Abnahme festgestellt?",
    "Was wurde bei der Abnahme beanstandet?",
    "Was hat die Prüfingenieurin bei der Abnahme bemängelt?",
    "Welche Mängel hat die Prüfingenieurin nach der Abnahme festgestellt?",
])
def test_what_is_still_to_be_done_was_not_found_at_it(env, question):
    r = _ask(env, question, "BS-2023-0412")
    assert r.status == "not_found", r.answer


def test_whether_there_were_defects_at_the_abnahme(env):
    r = _ask(env, "Wurden bei der Abnahme Mängel festgestellt?", "BS-2023-0412")
    assert r.status == "answered" and "Keine Mängel" in r.answer and "nachzuholen" not in r.answer, r.answer
    r = _ask(env, "Was ist nach der Abnahme der Feststellanlage zu tun?")  # was the rows of the Abnahmeprotokoll
    assert r.status == "not_found", r.answer


@pytest.mark.parametrize("text", [
    "## Feststellungen\nMangel: Die Laufkarten fehlen noch. Sie sind bis zur Abnahme nachzureichen.",
    "## Feststellungen\nMangel: Die Abnahme der Brandmeldeanlage steht noch aus.",
    "## Feststellungen\nMangel: Die Abnahme der Brandmeldeanlage ist noch nicht erfolgt.",
    "## Feststellungen\nMangel: Die Feststellanlage wurde ohne Abnahmeprüfung in Betrieb genommen. Die Abnahme durch "
    "eine Fachkraft ist nachzuholen.",
])
def test_occasions_in_synthetic_sources(text):
    assert _reader("Was wurde bei der Abnahme bemängelt?", (PB, text)) == NOT_FOUND


def test_before_a_milestone_is_a_deadline_only_for_an_obligation():
    assert _reader("Wie viele Gastbetten hatte das Hotel vor der Eröffnung?",
                   (DOC, "# 1 Allgemeines\nBei der Eröffnung hatte das Hotel 120 Gastbetten.")) == NOT_FOUND
    assert _reader("Was muss vor der Abnahme vorliegen?",
                   (DOC, "# 9 Prüfungen\nZur Abnahme müssen die Prüfbescheinigungen vorliegen.")) != NOT_FOUND
    assert _quals("Wie viele Gastbetten hatte das Hotel vor der Eröffnung?") == {("time", "vor", "eroffnung")}
    assert _quals("Was muss vor der Abnahme vorliegen?") == {("time", "bis", "abnahm")}
    assert _quals("Was ist bei der Abnahme vorzulegen?") == {("time", "bis", "abnahm")}
    assert _quals("Was ist nach der Abnahme zu tun?") == {("time", "ab", "abnahm")}
    assert _quals("Wie viele Betten hat das Hotel nach dem Umbau?") == {("time", "nach", "umbau")}


# --------------------------------------------------------------------------- 6. designators as written
def test_a_singular_identifier_is_no_range():
    room = (DOC, "# 3 Räume\nDer Raum 2-14 ist 40 m² groß.")
    assert _reader("Wie groß ist der Raum 5?", room) == NOT_FOUND
    assert _reader("Wie groß ist der Raum 2?", room) == NOT_FOUND
    assert _reader("Welche Klasse haben die Türen im Treppenraum 2?",
                   (DOC, "# 4 Türen\nDie Türen der Treppenräume 1-3 sind T30-RS.")) != NOT_FOUND


@pytest.mark.parametrize("question,text,answered", [
    ("Welche Klasse nach DIN EN 13501-2 haben die Wände?",
     "# 5 Bauteile\nDie Wände haben nach DIN EN 13501-1 die Klasse A2-s1,d0.", False),
    ("Welche Klasse nach DIN EN 13501-1 haben die Wände?",
     "# 5 Bauteile\nDie Wände haben nach DIN EN 13501-1 die Klasse A2-s1,d0.", True),
    ("Welche Handfeuermelder nach DIN EN 54-11 sind vorgesehen?",
     "# 6 Brandmeldeanlage\nEs sind Handfeuermelder nach DIN EN 54-13 vorgesehen.", False),
    ("Welche Handfeuermelder nach DIN EN 54-11 sind vorgesehen?",
     "# 6 Brandmeldeanlage\nEs sind Handfeuermelder nach DIN EN 54-11 vorgesehen.", True),
    ("Welche Anforderungen gelten nach DIN 4102?", "# 5 Bauteile\nDie Decken sind nach DIN 4102-2 in F90 auszuführen.",
     True),
])
def test_a_norms_part_is_part_of_its_designator(question, text, answered):
    assert (_reader(question, (DOC, text)) != NOT_FOUND) == answered
    assert ("designator", "13501-2", "en") in _quals("Welche Klasse nach DIN EN 13501-2 haben die Wände?")


def test_a_norms_part_in_demo(env):
    r = _ask(env, "Welche Anforderungen nach DIN EN 81-73 erfüllt die Steuerung des Feuerwehraufzugs?")
    assert r.status == "not_found", r.answer
    r = _ask(env, "Welche Norm erfüllt die Steuerung des Feuerwehraufzugs?")
    assert r.status == "answered" and "81-72" in r.answer, r.answer


# --------------------------------------------------------------------------- 7. findings, parties and requests
@pytest.mark.parametrize("text", [
    "Bitte senden Sie uns die Pläne zu. Wir müssen die Termine noch abstimmen.",
    "Die Besprechung ist erfolgt. Das Protokoll folgt.",
    "Der Termin muss verschoben werden, da ich im Urlaub bin.",
])
def test_an_organisational_sentence_is_no_finding(text):
    assert _reader("Was hat die Feuerwehr festgestellt?", (FW, text)) == NOT_FOUND


def test_an_assessment_is_still_a_finding():
    assert "ausreichend breit" in _reader("Was hat die Feuerwehr festgestellt?",
                                          (FW, "Die Feuerwehrzufahrt ist ausreichend breit."))


@pytest.mark.parametrize("question", [
    "Was hat die Bauaufsicht beim Hotel Seeblick festgestellt?",  # was the permit's cover line
    "Was hat die Bauaufsicht bei der Grundschule festgestellt?",
    "Was bemängelt die Feuerwehr beim Wohnhochhaus?",  # was the Prüfbericht's "Vorraum des Feuerwehraufzugs"
    "Was wünscht der Bauherr im 4. OG der Hafenkante?",  # was "Der Mietvertrag … ist unterschrieben"
])
def test_a_party_is_named_by_itself(env, question):
    r = _ask(env, question)
    assert r.status == "not_found", r.answer


def test_a_party_is_not_named_by_a_compound():
    report = (PB, "## Feststellungen\nDer Vorraum des Feuerwehraufzugs muss in allen Geschossen mindestens 6 m² "
                  "groß sein.")
    assert _reader("Was hat die Feuerwehr bemängelt?", report) == NOT_FOUND
    assert "6 m²" in _reader("Was hat die Prüfingenieurin bemängelt?", report)


def test_a_request_is_answered_by_a_wish_or_demand(env):
    r = _ask(env, "Was verlangt die Prüfingenieurin bei der Industriehalle?")
    assert r.status == "answered" and ("Brandwand" in r.answer or "Saugstelle" in r.answer), r.answer
    assert "Prüfingenieurbüro" not in r.answer and "liegt vor" not in r.answer, r.answer
    r = _ask(env, "Was schlägt die Feuerwehr bei der Kita vor?")
    assert r.status == "answered" and "Evakuierungsübung" in r.answer and "Baugenehmigung" not in r.answer, r.answer
    r = _ask(env, "Was hat der Prüfingenieur zur Druckerhöhungsanlage gefordert?")
    assert r.status == "answered" and "redundant" in r.answer, r.answer


# --------------------------------------------------------------------------- 8. dates and plans
def test_a_date_in_the_question_must_be_the_sentences(env):
    r = _ask(env, "Was ist am 13.12.2024 beim Pflegeheim abgenommen worden?")
    assert r.status == "not_found", r.answer
    r = _ask(env, "Was wurde in der Besprechung vom 05.12.2024 zum Hotel Seeblick vereinbart?")
    assert r.status == "not_found", r.answer
    r = _ask(env, "Was wurde am 12.12.2024 im Pflegeheim abgenommen?")
    assert r.status == "answered" and "Feststellanlage" in r.answer, r.answer
    assert _quals("Was ist am 13.12.2024 abgenommen worden?") == {("date", "2024-12-13", None)}


def test_a_date_in_synthetic_sources():
    record = ("[4] Abnahmeprotokoll.pdf (Anhang einer Mail vom 16.12.2024)" + P,
              "Abnahmeprotokoll Feststellanlage\nDatum der Abnahme: 12.12.2024\nErgebnis: Keine Mängel.")
    assert _reader("Was wurde am 13.12.2024 abgenommen?", record) == NOT_FOUND
    assert "12.12.2024" in _reader("Was wurde am 12.12.2024 abgenommen?", record)
    # a sentence without a date of its own takes its source's
    mail = ("[2] Mail vom 15.10.2024 (Dipl.-Ing. Eva Muster): Stichflur" + P,
            "Sehr geehrte Damen und Herren,\nder Abweichung kann ich nicht zustimmen.\nMit freundlichen Grüßen\n"
            "Dipl.-Ing. Eva Muster\nPrüfingenieurin für Brandschutz")
    assert "nicht zustimmen" in _reader("Was hat die Prüfingenieurin am 15.10.2024 geschrieben?", mail)
    assert _reader("Was hat die Prüfingenieurin am 16.10.2024 geschrieben?", mail) == NOT_FOUND


def test_a_proposal_is_no_event_that_took_place(env):
    r = _ask(env, "Wann wurde die Druckbelüftungsanlage der Hafenkante abgenommen?")  # was "… schlage ich … vor"
    assert r.status == "not_found", r.answer
    proposal = ("[2] Mail vom 31.12.2024 (Jonas Feld): Termin Abnahme" + P,
                "Hallo,\nals Termin für die Abnahme der Druckbelüftungsanlage schlage ich den 11. März vor.\n"
                "Gruß\nJonas")
    assert _reader("Wann wurde die Druckbelüftungsanlage abgenommen?", proposal) == NOT_FOUND


# --------------------------------------------------------------------------- 9. the party as the speaker
@pytest.mark.parametrize("question", [
    "Was hat die Feuerwehr zur Steuerung des Feuerwehraufzugs geschrieben?",  # was the elevator maker's document
    "Fasse zusammen, was die Feuerwehr beim Wohnhochhaus will",
    "Was hat die Feuerwehr zu den Feuerwehrplänen der Grundschule gesagt?",  # was "… mit dem Feuerwehr-Dreikant …"
])
def test_a_party_speaks_in_its_own_sources_or_as_the_subject(env, question):
    r = _ask(env, question)
    assert r.status == "not_found", r.answer


def test_what_the_party_said_is_still_found(env):
    r = _ask(env, "Was sagt die Feuerwehr zum Feuerwehraufzug beim Wohnhochhaus?")
    assert r.status == "answered" and "gemeinsam zu erproben" in r.answer and "81-72" not in r.answer, r.answer
    r = _ask(env, "Was hat die Feuerwehr zur Aufstellfläche an der Parkallee angemerkt?")
    assert r.status == "answered" and "12 m" in r.answer, r.answer


def test_the_speaker_in_synthetic_sources():
    permit = ("[1] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 02.07.2024)" + P,
              "Auflagen zum Brandschutz\nAuflage 9 (Feuerwehrpläne): Feuerwehrpläne sind mit der "
              "Brandschutzdienststelle abzustimmen.")
    assert _reader("Was hat die Feuerwehr gesagt?", permit) == NOT_FOUND
    maker = ("[2] Feuerwehraufzug_Steuerung.pdf (Anhang einer Mail vom 11.04.2025)" + P,
             "Steuerung Feuerwehraufzug\nDer Aufzug fährt bei Brandalarm in die Zugangsebene und wird dort von der "
             "Feuerwehr übernommen.")
    assert _reader("Was hat die Feuerwehr geschrieben?", maker) == NOT_FOUND
    protocol = (PROT, "Besprechungsprotokoll Nr. 2\nTOP 1 Zufahrt: Die Feuerwehr fordert eine zweite Zufahrt.\n"
                      "Aufgestellt: Murat Yilmaz")
    assert "zweite Zufahrt" in _reader("Was hat die Feuerwehr gefordert?", protocol)
    hearsay = (PROT, "Laut Feuerwehr ist eine zweite Zufahrt erforderlich.")
    assert "zweite Zufahrt" in _reader("Was hat die Feuerwehr gesagt?", hearsay)


# --------------------------------------------------------------------------- 10. long lines
def test_a_long_line_of_dates_is_split_in_linear_time():
    import time

    from projektakte.textutil import split_sentences
    assert split_sentences("Die Prüfung ist am 11.03. erfolgt. Nr. 2 folgt. Dipl.-Ing. Muster prüft.") == [
        "Die Prüfung ist am 11.03. erfolgt.", "Nr. 2 folgt.", "Dipl.-Ing. Muster prüft."]
    line = " ".join(["Ich schlage als Termin für die Abnahme den 11.03. vor"] * 3000)
    start = time.perf_counter()
    assert len(split_sentences(line)) == 1
    assert time.perf_counter() - start < 3.0  # was about 10 s: the growing buffer was searched again and again
