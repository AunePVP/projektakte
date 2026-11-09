"""The fake reader fails closed (round 2): a question for a value is only answered by a sentence with
such a value for the asked topic, decisions by the sentence stating them, "Was hat X geschrieben?"
by X's own mail; tables are read as "Spalte: Wert"; summaries collect several sources."""
from projektakte.fake_ai.core import NOT_FOUND, answer_from_sources

PROJ = " – Projekt BS-2099-0001 Garage Nord"
PERMIT_P1 = ("[1] 01 Genehmigung/Baugenehmigung.pdf, S. 1 (Stand 02.07.2024)" + PROJ, (
    "Untere Bauaufsicht Beispielstadt\nBauherr: Parkhaus Nord GmbH, vertreten durch Ingrid Vogt\n"
    "Bauvorhaben: Neubau einer Großgarage mit zwei Untergeschossen und 240 Einstellplätzen.\n"
    "Grundstück: Nordweg 1, 31888 Beispielstadt"))
CONCEPT = ("[2] 02 Konzept/Brandschutzkonzept.docx (Stand 01.06.2024)" + PROJ, (
    "# 2 Kennwerte\nKennwert | Wert\nEbenen | 2\nLuftwechsel Entrauchung | 12-fach\nLöschwasser | 96 m³/h\n"
    "# 8 Zusammenfassung\nBei Umsetzung der Maßnahmen bestehen keine Bedenken gegen das Bauvorhaben."))
FIRE = ("[3] Mail vom 11.11.2023 (Brandamtsrätin Nina Brück): Stellungnahme Brandschutzdienststelle – BS-2099-0001"
        + PROJ, (
            "Betreff: Stellungnahme Brandschutzdienststelle\nVon: Nina Brück <nb@feuerwehr.example>\n"
            "Sehr geehrte Damen und Herren,\nzum Bauvorhaben Garage Nord nehmen wir wie folgt Stellung: Die "
            "Einfahrtshöhe von 2,10 m ist für unsere Einsatzfahrzeuge nicht ausreichend.\nMit freundlichen Grüßen\n"
            "Nina Brück\nFeuerwehr Beispielstadt"))
REPORT = ("[4] 03 Prüfung/Prüfbericht Nr. 1.md (Stand 01.10.2024)" + PROJ, (
    "## Feststellungen\n1. Die Ventilatoren der Entrauchung sind für 300 °C über 60 Minuten auszulegen.\n"
    "2. Die Sprachalarmanlage ist in der Steuermatrix zu berücksichtigen."))
OWNER_OLD = ("[5] Mail vom 01.03.2024 (Ingrid Vogt): BS-2099-0001 – Zwischenstand" + PROJ, (
    "Betreff: Zwischenstand\nVon: Ingrid Vogt <iv@parkhaus.example>\nGuten Tag,\n"
    "die Ausschreibung der Entrauchung läuft noch.\nMit freundlichen Grüßen\nIngrid Vogt"))
OWNER_NEW = ("[6] Mail vom 05.07.2024 (Ingrid Vogt): BS-2099-0001 – aktueller Stand" + PROJ, (
    "Betreff: aktueller Stand\nVon: Ingrid Vogt <iv@parkhaus.example>\nGuten Tag,\n"
    "der Probebetrieb der Entrauchung ist gut gelaufen, die Eröffnung ist für den 1. September geplant.\n"
    "Mit freundlichen Grüßen\nIngrid Vogt"))
MINUTES = ("[7] 04 Protokolle/2024-06-22 Besprechungsprotokoll Nr. 5.txt (Stand 22.06.2024)" + PROJ, (
    "Besprechungsprotokoll Nr. 5\nTeilnehmer: Ingrid Vogt (Bauherr), Paul Hinz (Architektur)\nTOP 1 – Entrauchung\n"
    "Der Probebetrieb ist für Juli geplant.\nNächste Besprechung: 20.07.2024"))
PI = ("[8] Mail vom 15.10.2024 (Dipl.-Ing. Eva Muster): Abweichungsantrag Rampe – BS-2099-0001" + PROJ, (
    "Sehr geehrte Damen und Herren,\nder Abweichung zur Rampenneigung kann ich nicht zustimmen; der Antrag wird "
    "abgelehnt. Ich empfehle eine flachere Rampe.\nMit freundlichen Grüßen\nDipl.-Ing. Eva Muster\n"
    "Prüfingenieurin für Brandschutz"))
PERMIT_P2 = ("[9] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 02.07.2024)" + PROJ, (
    "Auflage 9 (Abweichungen): Über den Antrag auf Abweichung zur Rampenneigung wird nach Vorlage der "
    "Stellungnahme des Prüfingenieurs entschieden.\nAuflage 10 (Rettungswege): Von jeder Stelle eines "
    "Einstellplatzes muss in höchstens 30 m Entfernung ein Ausgang erreichbar sein."))
LEAD = ("[10] Mail vom 25.07.2024 (Petra Lind): Rauchwarnmelder" + PROJ, (
    "Guten Tag,\nfür die Garage fragt die Leitung, ob die Rauchwarnmelder auch im Büro Alarm geben. Petra Lind"))


def _ask(question, *sources):
    raw = [(int(h[1:h.index("]")]), h.split("] ", 1)[1], t) for h, t in sources]
    return answer_from_sources(question, raw)


ALL = (PERMIT_P1, CONCEPT, FIRE, REPORT, OWNER_OLD, OWNER_NEW, MINUTES, PI, PERMIT_P2, LEAD)


def test_counts_need_a_number_for_the_asked_thing():
    """The verifier's case: "Wie viele Stellplätze …?" was answered with "Einfahrtshöhe von 2,10 m"."""
    a = _ask("Wie viele Stellplätze hat die Garage?", FIRE, PERMIT_P1, PERMIT_P2)
    assert "240" in a and "2,10" not in a and "30 m" not in a
    assert _ask("Wie viele Rauchmelder hat die Garage?", *ALL) == NOT_FOUND


def test_questions_for_absent_values_are_not_found():
    """Kosten, Abnahmedatum, Hersteller of an existing topic: "Nicht gefunden.", not a foreign sentence."""
    for question in ("Was kostet die Sprachalarmanlage?", "Wie teuer wird die Entrauchung?",
                     "Wann wurde die Sprachalarmanlage abgenommen?", "Welcher Hersteller liefert die Ventilatoren?",
                     "Welche Farbe haben die Ventilatoren?", "Wie viele Ebenen hat das Pflegeheim?"):
        assert _ask(question, *ALL) == NOT_FOUND, question


def test_lufter_is_no_luftwechsel():
    a = _ask("Wie lange müssen die Lüfter der Entrauchung durchhalten?", CONCEPT, REPORT)
    assert "60 Minuten" in a and "12-fach" not in a


def test_tables_are_read_as_facts():
    a = _ask("Wie hoch ist die Löschwassermenge?", CONCEPT, PERMIT_P1)
    assert "Löschwasser: 96 m³/h" in a and "|" not in a
    assert "Ebenen: 2" in _ask("Welche Kennwerte hat die Garage?", CONCEPT)


def test_decisions_come_from_the_source_stating_them():
    """Not the conditional permit ("wird … entschieden"), but the Prüfingenieurin's rejection."""
    a = _ask("Hat die Prüfingenieurin dem Abweichungsantrag zugestimmt?", PERMIT_P2, PI)
    assert "abgelehnt" in a and "entschieden" not in a and "[8]" in a
    a = _ask("Wie wurde über die Abweichung zur Rampenneigung entschieden?", PERMIT_P2, PI)
    assert "nicht zustimmen" in a
    assert _ask("Hat die Feuerwehr der Abweichung zugestimmt?", PERMIT_P2, PI) == NOT_FOUND


def test_what_a_party_wrote_and_the_latest_news():
    a = _ask("Was gibt es Neues vom Bauherrn?", PERMIT_P1, OWNER_OLD, OWNER_NEW, MINUTES)
    assert a.startswith("Laut Mail von Ingrid Vogt vom 05.07.2024") and "Teilnehmer" not in a
    a = _ask("Was hat der Bauherr zuletzt geschrieben?", MINUTES, OWNER_OLD, OWNER_NEW)
    assert "05.07.2024" in a and "Teilnehmer" not in a
    a = _ask("Was hat die Feuerwehr gesagt?", PERMIT_P2, FIRE)
    assert "Einfahrtshöhe" in a and "[3]" in a and "nehmen wir wie folgt" not in a


def test_adjectives_and_short_stems_do_not_match_other_words():
    """"offen" is no "Offenhalten"/"öffentlich"; an adjective of a filler noun is no topic."""
    doors = ("[11] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 02.07.2024)" + PROJ,
             "Auflage 8 (Türen): Keile und Haken zum Offenhalten sind unzulässig.\n"
             "Auflage 9 (Löschwasser): 96 m³/h aus dem öffentlichen Netz.")
    assert _ask("Welche offenen Punkte gibt es noch?", doors) == NOT_FOUND
    assert _ask("Was ist noch offen?", doors) == NOT_FOUND


def test_participant_lists_and_indirect_questions_are_no_answer():
    assert "Teilnehmer" not in _ask("Was macht der Bauherr?", MINUTES, PERMIT_P1)
    assert _ask("Welche Rauchwarnmelder kommen in die Garage?", LEAD) == NOT_FOUND
    assert "Ingrid Vogt (Bauherr)" in _ask("Wer nahm an der Besprechung teil?", MINUTES)


def test_summary_collects_several_sources():
    a = _ask("Fasse das Projekt Garage Nord zusammen.", MINUTES, CONCEPT, PERMIT_P1, OWNER_NEW)
    # the closing formula ("… bestehen keine Bedenken gegen das Bauvorhaben") is no content (round 5)
    assert "Großgarage" in a and "Probebetrieb" in a and "keine Bedenken" not in a and a.count("[") >= 2


def test_dates_need_the_asked_event():
    """"Wann wird X abgenommen?" is not answered by the date of another event of X."""
    assert _ask("Wann wird die Entrauchung abgenommen?", MINUTES, OWNER_NEW) == NOT_FOUND
    assert "20.07.2024" in _ask("Wann ist die nächste Baubesprechung?", MINUTES)
