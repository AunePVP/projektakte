"""Answers over the demo data, round 6: "bemängelt" is answered by a defect only (not by a wish with "ohne",
a permit's conditions or its standard notes); an ordinal names the asked one ("der erste Rettungsweg", "im
2. Bauabschnitt"); the fake embedding carries the reading of a question form, so "Wer hat das Konzept
geschrieben?" finds the "Verfasser:" line again; fields are named by their labels ("Datum:", "Betreff:"),
which a verb ("betrifft") does not match; "Was hat die Feuerwehr festgestellt?" quotes her assessments; a
signature written after a "Label: …" sentence is split off; qualifiers are read in the forms texts use
("im 3. und 4. OG", "im OG 3", "in allen Geschossen", "DIN 4102-2", "zur Abnahme", "vorher" as "in advance");
a list line after an unpunctuated sentence is no section heading of a snippet."""
import math
import sqlite3

import pytest

from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.demo import generate
from projektakte.fake_ai.core import (NOT_FOUND, active_synonyms, answer_from_sources, embed_text, question_qualifiers,
                                      question_terms, read_units, rerank_score, _parse_header)
from projektakte.indexer import run_index
from projektakte.textutil import plain_lines, relevant_passage, _passage_headings

from .conftest import fake_settings


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("antworten_r6")
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


P = " – Projekt BS-2099-0009 Seniorenheim Lindenallee"
DOC = "[1] 02 Konzept/Brandschutzkonzept.docx (Stand 01.02.2024)" + P
MAIL = "[2] Mail vom 05.03.2024 (Erika Muster): Abnahme" + P


# --------------------------------------------------------------------------- 1. criticism is a defect
@pytest.mark.parametrize("question", [
    "Was hat der Architekt bei der Hafenkante bemängelt?",  # "… wünscht ein Großraumbüro ohne Flurtrennwände."
    "Was hat der Architekt beim Bürogebäude Hafenkante 7 beanstandet?",
    "Was hat die Bauaufsicht beim Hotel Seeblick bemängelt?",  # the permit's standard notes
    "Was hat die Bauaufsicht bei der Kita bemängelt?",
    "Was hat die Bauaufsicht bei der Tiefgarage bemängelt?",
    "Was wurde bei der Abnahme der Feststellanlage im Pflegeheim bemängelt?",  # the Abnahme found no defect
])
def test_no_defect_no_criticism(env, question):
    """All were answered (green) with a sentence that merely contains "ohne", "nicht" or an obligation."""
    r = _ask(env, question)
    assert r.status == "not_found", r.answer


@pytest.mark.parametrize("question,facts", [
    ("Was hat die Prüfingenieurin beim Projekt Pflegeheim Sonnenhang bemängelt?",
     ("ohne Abnahmeprüfung in Betrieb genommen", "nachzuholen")),
    ("Was hat die Prüfingenieurin beim Projekt Grundschule Am Lindenhof bemängelt?", ("Außentreppe", "Steuermatrix")),
    ("Was hat die Prüfingenieurin beim Hotel Seeblick bemängelt?", ("zulässige Maß von 15 m",)),
])
def test_the_reviewers_defects_are_criticism(env, question, facts):
    r = _ask(env, question)
    assert r.status == "answered" and all(f in r.answer for f in facts), r.answer
    assert "liegt vor" not in r.answer and "bescheinigt" not in r.answer, r.answer


def test_a_defect_in_a_partys_own_words():
    mail = ("[1] Mail vom 15.10.2024 (Dipl.-Ing. Eva Muster): Kompensation Stichflur" + P,
            "Sehr geehrte Damen und Herren,\ndie vorgeschlagene Kompensation durch zusätzliche Rauchmelder reicht "
            "nicht aus. Der Mieter wünscht ein Großraumbüro ohne Flurtrennwände.\nMit freundlichen Grüßen\n"
            "Dipl.-Ing. Eva Muster\nPrüfingenieurin für Brandschutz")
    a = _reader("Was hat die Prüfingenieurin bemängelt?", mail)
    assert "reicht nicht aus" in a and "Großraumbüro" not in a, a
    wish = ("[1] Mail vom 13.06.2024 (Jonas Feld): Mieterausbau" + P,
            "Hallo Murat,\nder Mieter im 4. OG wünscht ein Großraumbüro ohne Flurtrennwände.\nViele Grüße\nJonas Feld\n"
            "Architekt")
    assert _reader("Was hat der Architekt bemängelt?", wish) == NOT_FOUND
    permit = ("[1] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 02.07.2024)" + P, (
        "Auflagen zum Brandschutz\nAuflage 8 (Brandschutztüren): Keile und Haken zum Offenhalten sind unzulässig.\n"
        "Hinweise\nDer Beginn der Bauarbeiten ist der Bauaufsicht mindestens eine Woche vorher anzuzeigen.\n"
        "Die Baugenehmigung erlischt, wenn innerhalb von drei Jahren nicht begonnen wurde."))
    assert _reader("Was hat die Bauaufsicht bemängelt?", permit) == NOT_FOUND


def test_no_defects_answer_whether_there_were_any():
    record = ("[1] Abnahmeprotokoll Feststellanlage.pdf (Anhang einer Mail vom 16.12.2024)" + P,
              "Abnahmeprotokoll Feststellanlage\nDatum der Abnahme: 12.12.2024\n"
              "Ergebnis: Die Feststellanlage löst bei Raucherkennung zuverlässig aus. Keine Mängel.")
    assert _reader("Was wurde bei der Abnahme bemängelt?", record) == NOT_FOUND
    a = _reader("Wurden bei der Abnahme Mängel festgestellt?", record)
    assert "Keine Mängel" in a and "12.12.2024" not in a, a
    # "Feststellanlage" is no "festgestellt": the date of the Abnahme is no finding
    assert "12.12.2024" not in _reader("Was wurde bei der Abnahme festgestellt?", record)


def test_a_statements_closing_formula_is_a_formula():
    statement = ("[1] Stellungnahme Feuerwehr.pdf (Anhang einer Mail vom 27.02.2024)" + P,
                 "Stellungnahme der Brandschutzdienststelle\nDie Aufstellfläche ist ausreichend.\n"
                 "Gegen das Bauvorhaben bestehen bei Beachtung dieser Hinweise keine Bedenken.")
    units = {u.text: u.formula for u in _units(statement)}
    assert units["Gegen das Bauvorhaben bestehen bei Beachtung dieser Hinweise keine Bedenken."] is True
    assert units["Die Aufstellfläche ist ausreichend."] is False


# --------------------------------------------------------------------------- 2. what the Feuerwehr found
@pytest.mark.parametrize("question", ["Was hat die Feuerwehr bei der Grundschule festgestellt?",
                                      "Was stellte die Feuerwehr bei der Grundschule fest?"])
def test_what_a_party_found_in_its_own_statement(env, question):
    r = _ask(env, question)
    assert r.status == "answered" and "Drehleiter an der Westseite ist ausreichend" in r.answer, r.answer
    assert "keine Bedenken" not in r.answer, r.answer


def test_a_cover_line_is_no_finding():
    cover = ("[1] Mail vom 11.09.2024 (Dipl.-Ing. Eva Muster): Prüfbericht Nr. 2" + P,
             "Sehr geehrte Damen und Herren,\nmein Prüfbericht Nr. 2 vom 09.09.2024 liegt vor.\n"
             "Mit freundlichen Grüßen\nDipl.-Ing. Eva Muster\nPrüfingenieurin für Brandschutz")
    assert _reader("Was hat die Prüfingenieurin festgestellt?", cover) == NOT_FOUND
    done = ("[1] Mail vom 16.12.2024 (Matthias Kortmann): Feststellanlage" + P,
            "Hallo,\ndie Abnahme der Feststellanlage im Wohnbereich B ist am 12.12.2024 erfolgt.\nGruß\n"
            "Matthias Kortmann\nBauherr")
    assert "erfolgt" in _reader("Was hat der Bauherr festgestellt?", done)


def test_a_label_inside_a_line_names_the_rest_of_it():
    mail = ("[1] Mail vom 14.11.2024 (Dipl.-Ing. Eva Muster): Prüfbericht Nr. 3" + P,
            "Sehr geehrte Damen und Herren,\nmein Prüfbericht Nr. 3 liegt vor. Wesentliche Feststellung: Mangel: Die "
            "Feststellanlage im 2. Obergeschoss wurde ohne Abnahmeprüfung in Betrieb genommen. Die Abnahme durch eine "
            "Fachkraft ist nachzuholen.\nMit freundlichen Grüßen\nDipl.-Ing. Eva Muster\nPrüfingenieurin für Brandschutz")
    labels = {u.text: u.label for u in _units(mail)}
    assert labels["Die Abnahme durch eine Fachkraft ist nachzuholen."] == "Wesentliche Feststellung"
    assert labels["Mein Prüfbericht Nr. 3 liegt vor."] is None
    a = _reader("Was hat die Prüfingenieurin bemängelt?", mail)
    assert "ohne Abnahmeprüfung" in a and "nachzuholen" in a and "liegt vor" not in a, a


# --------------------------------------------------------------------------- 3. ordinals
def test_the_first_escape_route_is_not_the_second(env):
    r = _ask(env, "Wohin führt der erste Rettungsweg der Unterrichtsräume im Obergeschoss der Grundschule?")
    assert r.status == "not_found", r.answer
    r = _ask(env, "Wohin führt der zweite Rettungsweg der Unterrichtsräume im Obergeschoss der Grundschule?")
    assert r.status == "answered" and "Außentreppe" in r.answer, r.answer


@pytest.mark.parametrize("question,text,answered", [
    ("Wann wurde die Brandmeldeanlage im 2. Bauabschnitt abgenommen?",
     "Die Brandmeldeanlage im Bauabschnitt 1 wurde am 03.03.2024 abgenommen.", False),
    ("Wann wurde die Brandmeldeanlage im Bauabschnitt 2 abgenommen?",
     "Die Brandmeldeanlage im 1. Bauabschnitt wurde am 03.03.2024 abgenommen.", False),
    ("Wann wurde die Brandmeldeanlage im zweiten Bauabschnitt abgenommen?",
     "Die Brandmeldeanlage im Bauabschnitt 2 wurde am 03.03.2024 abgenommen.", True),
    ("Wann wurde die Brandmeldeanlage im Bauabschnitt 2 abgenommen?",
     "Die Brandmeldeanlage im 2. Bauabschnitt wurde am 03.03.2024 abgenommen.", True),
])
def test_ordinal_and_number_designate_alike(question, text, answered):
    a = _reader(question, (MAIL, text))
    assert (a != NOT_FOUND) == answered, a


def test_ordinals_in_synthetic_sources():
    assert _reader("Wie groß ist der 2. Brandabschnitt?",
                   (DOC, "# 4 Brandabschnitte\nDer Brandabschnitt 1 ist 2.400 m² groß.")) == NOT_FOUND
    assert _reader("Wohin führt der 2. Rettungsweg?",
                   (DOC, "# 3 Rettungswege\nDer 1. Rettungsweg führt über den Treppenraum.")) == NOT_FOUND
    a = _reader("Wohin führt der erste Rettungsweg?", (DOC, "# 3 Rettungswege\nDer 1. Rettungsweg führt über den "
                                                             "Treppenraum. Ein zweiter baulicher Rettungsweg führt über "
                                                             "die Außentreppe."))
    assert "Treppenraum" in a and "Außentreppe" not in a, a


def test_question_qualifiers_r6():
    syn = active_synonyms("")

    def quals(q):
        return {(x.kind, x.value, x.head.term if x.head else None, x.group)
                for x in question_qualifiers(q, question_terms(q, syn), set())}

    assert quals("Wohin führt der erste Rettungsweg?") == {("designator", "1", "rettungsweg", "")}
    assert quals("Wie groß ist der 2. Brandabschnitt?") == {("designator", "2", "brandabschnitt", "")}
    assert quals("Was muss bis zur Abnahme vorliegen?") == {("time", "bis", "abnahm", "")}
    assert quals("Was wurde bei der Abnahme festgestellt?") == {("time", "zu", "abnahm", "")}
    assert quals("Wie viele Betten gab es früher?") == {("time", "vor", None, "")}
    # "vorher" in an obligation means "in advance", several floors are no single qualifier
    assert quals("Muss die Abnahme vorher angemeldet werden?") == set()
    assert quals("Welche Türen gibt es im 3. und 4. OG?") == set()
    assert quals("Welche Anforderungen gelten nach DIN 4102?") == {("designator", "4102", "din", "")}


# --------------------------------------------------------------------------- 4. qualifiers in the forms texts use
@pytest.mark.parametrize("question,text", [
    ("Welche Feuerwiderstandsklasse haben die Rauchschutztüren im 3. Obergeschoss?",
     "# 4 Türen\nDie Rauchschutztüren im 3. und 4. Obergeschoss sind als T30-RS ausgeführt."),
    ("Welche Feuerwiderstandsklasse haben die Rauchschutztüren im 3. Obergeschoss?",
     "# 4 Türen\nDie Rauchschutztüren im 2. bis 4. Obergeschoss sind als T30-RS ausgeführt."),
    ("Welche Feuerwiderstandsklasse haben die Rauchschutztüren im 3. OG?",
     "# 4 Türen\nDie Rauchschutztüren im OG 3 sind als T30-RS ausgeführt."),
    ("Welche Feuerwiderstandsklasse haben die Rauchschutztüren im 3. OG?",
     "# 4 Türen\nDie Rauchschutztüren in allen Obergeschossen sind als T30-RS ausgeführt."),
    ("Welche Anforderungen gelten nach DIN 4102?", "# 5 Bauteile\nDie Decken sind nach DIN 4102-2 in F90 auszuführen."),
    ("Welche Klasse haben die Türen im Treppenraum 2?", "# 4 Türen\nDie Türen der Treppenräume 1-3 sind T30-RS."),
    ("Was muss bis zur Abnahme vorliegen?",
     "# 9 Prüfungen\nZur Abnahme müssen die Prüfbescheinigungen der Sachverständigen vorliegen."),
    ("Muss die Abnahme vorher angemeldet werden?",
     "# 9 Abnahme\nDie Abnahme ist eine Woche im Voraus bei der Bauaufsicht anzumelden."),
    ("Wie lange vorher ist die Abnahme anzumelden?",
     "# 9 Abnahme\nDie Abnahme ist eine Woche im Voraus bei der Bauaufsicht anzumelden."),
    ("Wie viele Betten gab es früher?", "# 1 Allgemeines\nDas Heim hatte ursprünglich 80 Betten."),
])
def test_a_qualifier_named_in_another_form_still_answers(question, text):
    """All were refused: only the last number before a floor noun was read, a part number cancelled a norm,
    "bis zur" required "vor", "vorher" was read as the earlier state."""
    assert _reader(question, (DOC, text)) != NOT_FOUND


@pytest.mark.parametrize("question,text", [
    ("Welche Feuerwiderstandsklasse haben die Rauchschutztüren im 5. Obergeschoss?",
     "# 4 Türen\nDie Rauchschutztüren im 2. bis 4. Obergeschoss sind als T30-RS ausgeführt."),
    ("Welche Feuerwiderstandsklasse haben die Rauchschutztüren im 2. OG?",
     "# 4 Türen\nDie Rauchschutztüren im OG 3 sind als T30-RS ausgeführt."),
    ("Welche Klasse haben die Türen im Treppenraum 4?", "# 4 Türen\nDie Türen der Treppenräume 1-3 sind T30-RS."),
    ("Wie viele Betten gab es früher?", "# 1 Allgemeines\nDas Heim hat 96 Betten."),
    ("Was muss bis zur Abnahme vorliegen?",
     "# 9 Prüfungen\nNach der Abnahme müssen die Prüfbescheinigungen vorliegen."),
])
def test_another_value_of_the_qualifier_is_still_refused(question, text):
    assert _reader(question, (DOC, text)) == NOT_FOUND


# --------------------------------------------------------------------------- 5. authors
@pytest.mark.parametrize("question,project,author", [
    ("Wer hat das Konzept fürs Heim geschrieben?", None, "Lea Funke"),
    ("Wer hat das Konzept geschrieben?", "BS-2024-0233", "Murat Yilmaz"),
    ("Wer hat das Brandschutzkonzept geschrieben?", "BS-2024-0233", "Murat Yilmaz"),
    ("Wer hat das Konzept geschrieben?", "BS-2022-0901", "Tobias Brenner"),
    ("Wer hat das Brandschutzkonzept für das Hotel Seeblick verfasst?", None, "Murat Yilmaz"),
])
def test_who_wrote_the_concept_is_found_again(env, question, project, author):
    """Round 5 moved "geschrieben → Verfasser" out of the synonym list; the reading only reached the reader, so
    the "Verfasser:" chunk never reached it (or the answer was "… mit dem Verfasser abzustimmen")."""
    r = _ask(env, question, project)
    assert r.status == "answered" and f"Verfasser: Ingenieurbüro Feuerfest, {author}" in r.answer, r.answer


def test_who_wrote_a_report_needs_a_document_called_so(env):
    r = _ask(env, "Wer hat den Bericht geschrieben?")  # was "… berichten wir über …" from a newsletter
    assert "Fachverband" not in r.answer and "berichten" not in r.answer, r.answer


def test_the_query_embedding_carries_the_question_forms_reading():
    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))

    author = "Verfasser: Ingenieurbüro Feuerfest, Lea Funke. Stand: 27.07.2023"
    other = "Rettungswege: Horizontale Evakuierung in den benachbarten Rauchabschnitt."
    query = embed_text("Instruct: Finde Abschnitte\nQuery: Wer hat das Konzept geschrieben?")
    assert cos(query, embed_text(author)) > cos(query, embed_text(other)) + 0.05
    # without the query instruction a text is embedded as it stands (documents never get a reading)
    plain = embed_text("Wer hat das Konzept geschrieben?")
    assert cos(plain, embed_text(author)) < cos(query, embed_text(author))
    findings = embed_text("Instruct: x\nQuery: Was hat die Prüfingenieurin bemängelt?")
    assert cos(findings, embed_text("Feststellungen: Die Brandwand ist über Dach zu führen.")) > 0.1


def test_the_author_reading_is_the_label_not_the_verb_stem():
    syn = active_synonyms("")
    protocol = "Besprechungsprotokoll Nr. 2\nAufgestellt: Murat Yilmaz"
    rule = "Auflage 4 (Brandschutzordnung): Eine Brandschutzordnung ist aufzustellen; Aufstellflächen sind frei zu halten."
    assert rerank_score("Wer hat das Protokoll erstellt?", protocol, syn) > rerank_score(
        "Wer hat das Protokoll erstellt?", rule, syn)
    assert rerank_score("Wer hat den Bericht geschrieben?", rule, syn) == 0


# --------------------------------------------------------------------------- 6. fields and their labels
PROT = ("[1] 04 Protokolle/Besprechungsprotokoll Nr. 3.txt (Stand 05.12.2024)" + P,
        "Besprechungsprotokoll Nr. 3\nDatum: 05.12.2024, Ort: Baustelle\nTeilnehmer: Erika Muster (Bauherr)\n"
        "TOP 1 – Rauchmelder\nDie Rauchmelder werden im Januar montiert.")
LETTER = ("[1] 06 Schriftverkehr/Schreiben Bauaufsicht.pdf, S. 1 (Stand 05.12.2024)" + P,
          "Bauaufsicht Musterstadt\nBetreff: Nachforderung Brandschutznachweis\nSehr geehrte Damen und Herren,\n"
          "bitte reichen Sie den Brandschutznachweis nach.")


def test_a_field_is_named_by_its_label():
    assert "05.12.2024" in _reader("Welches Datum hat das Besprechungsprotokoll?", PROT)
    assert "05.12.2024" in _reader("An welchem Datum war die Besprechung?", PROT)
    assert "Nachforderung Brandschutznachweis" in _reader("Welchen Betreff hat das Schreiben der Bauaufsicht?", LETTER)


def test_demo_protocol_date(env):
    r = _ask(env, "Welches Datum hat das Besprechungsprotokoll Nr. 7?", "BS-2023-0412")
    assert r.status == "answered" and "05.12.2024" in r.answer, r.answer


def test_betrifft_is_a_verb_not_the_subject_label():
    syn = active_synonyms("")
    for doc in ("> Betreff: Wartung der Kaffeemaschine\n> Von: Hausverwaltung\nDie Maschine wird gewartet.",
                "| Betreff | Wartung der Kaffeemaschine |\n|---|---|\nDie Maschine wird gewartet.",
                "Wartung\nBetreff: Kaffeemaschine"):
        for q in ("Wen betrifft die Räumungsübung?", "Welche Mängel betrifft die Nachprüfung?"):
            assert rerank_score(q, doc, syn) == 0, (q, doc)
    # the verb in the text still counts
    assert rerank_score("Welche Auflage betrifft die Geschosse?",
                        "Auflage 6 (Feuerlöscher): Die Auflagen betreffen alle Geschosse.", syn) > 0.9
    letter = ("[1] Schreiben Bauamt.pdf (Stand 01.03.2025)" + P,
              "| Betreff | Wartung der Kaffeemaschine |\n|---|---|\nDie Maschine wird am Freitag gewartet.")
    assert _reader("Wen betrifft die Räumungsübung?", letter) == NOT_FOUND


# --------------------------------------------------------------------------- 7. signatures
def test_a_signature_after_a_labelled_sentence_is_split_off():
    mail = ("[1] Mail vom 05.03.2024 (Benedikt Sauer): Bäume" + P,
            "Sehr geehrte Damen und Herren,\ndie Bäume sollen erhalten bleiben.\n"
            "Hinweis: Die Unterlagen folgen per Post. Benedikt Sauer, Wohnbau Stadtpark eG")
    a = _reader("Wer hat die Mail zu den Bäumen unterschrieben?", mail)
    assert "Unterschrift: Benedikt Sauer, Wohnbau Stadtpark eG" in a, a
    texts = [u.text for u in _units(mail) if u.kind != "signature"]
    assert "Die Unterlagen folgen per Post." in texts and not any("Wohnbau" in t for t in texts), texts
    # a field stays one value, the legend of a plan is no signature
    team = ("[1] 04 Protokolle/Besprechungsprotokoll Nr. 1.txt (Stand 01.03.2025)" + P,
            "Die Rampe wird verlängert.\nTeilnehmer: Ingrid Vogt (Bauherr). Paul Hinz, Strebewerk GmbH")
    assert not [u for u in _units(team) if u.kind == "signature"]
    plan = ("[1] 05 Pläne/Fluchtwegplan OG.png, S. 1 (Stand 01.03.2025)" + P,
            "Transkription: Fluchtwegplan Obergeschoss. Notausgang West, Sammelplatz Wiese Süd, Wandhydrant neben "
            "Aufzug 2.")
    assert [u.text for u in _units(plan)] == ["Notausgang West", "Sammelplatz Wiese Süd", "Wandhydrant neben Aufzug 2"]


# --------------------------------------------------------------------------- 8. snippet headings
def test_a_list_line_after_an_unpunctuated_sentence_is_no_heading():
    text = "Im Treppenhaus Nord sind je Geschoss vorzusehen\n2 Pulverlöscher\nDie Löscher sind bis zum 30.06.2025 zu montieren."
    p = relevant_passage(text, "Bis wann sind die Löscher im Treppenhaus Nord zu montieren?",
                         "Die Löscher sind bis zum 30.06.2025 zu montieren.")
    assert p.startswith("Im Treppenhaus Nord") and "30.06.2025" in p, p
    assert _passage_headings(text, plain_lines(text)) == set()
    # a title before a numbered section heading leaves the heading one
    for text, heading in (("Brandschutzkonzept\n7 Organisatorischer Brandschutz\nNachts drei Pflegekräfte.",
                           "7 Organisatorischer Brandschutz"),
                          ("Prüfbericht Nr. 3 zum Brandschutznachweis\n2 Gegenstand\nGeprüft wurde das Konzept.",
                           "2 Gegenstand")):
        lines = plain_lines(text)
        assert {lines[i] for i in _passage_headings(text, lines)} == {heading}, text
