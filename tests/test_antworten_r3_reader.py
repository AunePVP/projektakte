"""Fake reader, round 3: no confident wrong answers from a mixed-up dimension ("Wie breit …?" is not
answered by "in 60 cm Höhe"), from an obligation or plan for a past event ("Wann wurde … abgenommen?"
is not answered by "… ist vor der Abnahme vorzulegen"), from another project than the one named, from
courtesy phrases or role lines; overview questions in many phrasings; the signature answers "Wer hat …
unterschrieben?" and nothing else; SYNONYMS_FILE reaches reranker and reader; numbers alone are no
topic for the reranker."""
import json
import urllib.request

from projektakte.fake_ai.core import NOT_FOUND, active_synonyms, answer_from_sources, rerank_score

P1 = " – Projekt BS-2099-0001 Kita Sonnenblume"
P2 = " – Projekt BS-2099-0002 Bürogebäude Hafenplatz 3"
P3 = " – Projekt BS-2099-0003 Tiefgarage Rathausplatz"


def _ask(question, *sources, synonyms=None):
    raw = [(int(h[1:h.index("]")]), h.split("] ", 1)[1], t) for h, t in sources]
    return answer_from_sources(question, raw, synonyms)


HANDRAIL = ("[1] Mail vom 15.06.2025 (Jonas Feld): Handlauf Balkontreppe" + P1, (
    "Betreff: Handlauf Balkontreppe\nVon: Jonas Feld <jf@example.org>\nHallo Murat,\n"
    "zum Projekt BS-2099-0001 (Kita Sonnenblume): Der kindgerechte Handlauf an der Balkontreppe soll in 60 cm "
    "Höhe montiert werden.\nJonas Feld"))
PERMIT = ("[2] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 28.09.2023)" + P1, (
    "Auflage 1 (Feuerwehrpläne): Feuerwehrpläne nach DIN 14095 sind vor Nutzungsaufnahme mit der "
    "Brandschutzdienststelle abzustimmen und in zweifacher Ausfertigung zu übergeben.\n"
    "Auflage 2 (Rauchabschnitte): Die Flure der Wohnbereiche sind durch Rauchschutztüren in Rauchabschnitte von "
    "höchstens 30 m Länge zu unterteilen.\n"
    "Auflage 3 (Brandmeldeanlage): Die Laufkarten der Brandmeldeanlage sind vor der Abnahme vorzulegen."))
CONCEPT = ("[3] 02 Konzept/Brandschutzkonzept.docx (Stand 14.01.2025)" + P1, (
    "# 1 Allgemeines\nNeubau einer fünfgruppigen Kindertagesstätte mit Erdgeschoss und Obergeschoss.\n"
    "# 2 Kennwerte\nKennwert | Wert\nStichflur OG | 17 m\nÜberdruck Treppenraum | 15–50 Pa\n"
    "# 3 Rettungswege\nDer Flur im Obergeschoss ist 38 m lang. Die Außentreppe hat 1,20 m nutzbare Breite.\n"
    "# 5 Löschwasserversorgung\nDer Löschwasserbedarf beträgt 96 m³/h über 2 Stunden. Er wird aus einem "
    "Löschwasserteich mit 200 m³ gedeckt.\n"
    "# 8 Zusammenfassung\nBei Umsetzung der Maßnahmen bestehen keine Bedenken gegen das Bauvorhaben."))
MINUTES = ("[4] 04 Protokolle/2025-02-20 Besprechungsprotokoll Nr. 4.txt (Stand 20.02.2025)" + P1, (
    "Besprechungsprotokoll Nr. 4\nTeilnehmer: Petra Lind (Bauherr), Jonas Feld (Architektur)\n"
    "TOP 1 – Brandmeldeanlage\nDie Abnahme der Brandmeldeanlage durch den Prüfsachverständigen ist für den "
    "18.06.2025 geplant."))


def test_dimension_questions_need_the_asked_dimension():
    """The verifier's cases: width ← height of a handrail, length of the corridor ← length of the smoke
    compartments, width of a corridor ← a bare '17 m', depth of a pond ← its volume."""
    assert _ask("Wie breit ist die Balkontreppe?", HANDRAIL, PERMIT, CONCEPT) == NOT_FOUND
    assert "60 cm" in _ask("Wie hoch soll der Handlauf an der Balkontreppe sein?", HANDRAIL, CONCEPT)
    assert _ask("Wie breit ist der Stichflur?", CONCEPT) == NOT_FOUND
    assert _ask("Wie tief ist der Löschwasserteich?", CONCEPT) == NOT_FOUND
    a = _ask("Wie lang ist der Flur?", PERMIT, CONCEPT)
    assert "38 m" in a and "30 m Länge" not in a
    assert _ask("Wie lang sind die Flure der Wohnbereiche?", PERMIT) == NOT_FOUND  # 30 m belongs to the compartments
    assert _ask("Wie hoch ist die Außentreppe?", CONCEPT) == NOT_FOUND
    assert "1,20 m" in _ask("Wie breit ist die Außentreppe?", CONCEPT)
    assert "15–50 Pa" in _ask("Wie hoch ist der Überdruck im Treppenraum?", CONCEPT)  # no length: unaffected


def test_a_value_of_the_project_itself_needs_the_project_in_the_sentence():
    garage = ("[1] 04 Protokolle/Besprechungsprotokoll Nr. 5.txt (Stand 22.06.2023)" + P3,
              "TOP 2 – CO-Warnanlage\nDie Sensoren werden auf 1,5 m Höhe montiert.")
    assert _ask("Wie hoch ist die Tiefgarage?", garage) == NOT_FOUND
    tower = ("[1] 02 Konzept/Brandschutzkonzept.docx" + " – Projekt BS-2099-0004 Wohnhochhaus Am Park",
             "# 1 Allgemeines\nNeubau eines Wohnhochhauses mit 22 Geschossen, Höhe 68 m.")
    assert "68 m" in _ask("Wie hoch ist das Wohnhochhaus?", tower)


def test_past_events_need_a_date_of_the_event_not_an_obligation_or_plan():
    """"Wann wurde die BMA abgenommen?" was answered with "Die Laufkarten … sind vor der Abnahme
    vorzulegen" – an obligation is no event; neither is a plan ("ist für den 18.06.2025 geplant")."""
    assert _ask("Wann wurde die Brandmeldeanlage abgenommen?", PERMIT, MINUTES) == NOT_FOUND
    assert _ask("Wann wurde die BMA abgenommen?", PERMIT, MINUTES) == NOT_FOUND
    assert _ask("Wann wurden die Feuerwehrpläne übergeben?", PERMIT) == NOT_FOUND
    assert "Nutzungsaufnahme" in _ask("Wann sind die Feuerwehrpläne zu übergeben?", PERMIT)  # asks for the duty
    assert "18.06.2025" in _ask("Wann ist die Abnahme der Brandmeldeanlage geplant?", PERMIT, MINUTES)
    done = ("[5] Mail vom 16.12.2024 (Petra Lind): aktueller Stand" + P1,
            "Guten Tag,\ndie Abnahme der Brandmeldeanlage ist am 12.12.2024 erfolgt.\nPetra Lind")
    assert "12.12.2024" in _ask("Wann wurde die Brandmeldeanlage abgenommen?", PERMIT, MINUTES, done)


def test_relative_time_is_no_answer_for_its_own_event():
    """"vor der Abnahme" does not say when the Abnahme is."""
    assert _ask("Wann ist die Abnahme der Brandmeldeanlage?", PERMIT) == NOT_FOUND
    assert "vor der Abnahme" in _ask("Wann sind die Laufkarten vorzulegen?", PERMIT)


def test_completion_question_is_not_answered_by_an_obligation():
    assert _ask("Ist die Brandmeldeanlage schon abgenommen?", PERMIT) == NOT_FOUND


def test_answer_only_from_the_named_project():
    """"Tiefgarage" also names the project "Tiefgarage Rathausplatz" – but the question names the
    Bürogebäude Hafenplatz with two words: answer from that project or not at all."""
    office = ("[1] 02 Konzept/Brandschutzkonzept.docx" + P2,
              "# 1 Allgemeines\nNeubau eines Bürogebäudes mit Tiefgarage, Gebäudehöhe 21,5 m.")
    garage = ("[2] 02 Konzept/Brandschutzkonzept.docx" + P3,
              "# 3 Rettungswege\nDie lichte Einfahrtshöhe von 2,10 m ist mit der Feuerwehr abgestimmt.\n"
              "# 4 Technik\nLuftwechsel Entrauchung: 12-fach.")
    assert _ask("Wie hoch ist die Einfahrt der Tiefgarage des Bürogebäudes Hafenplatz?", office, garage) == NOT_FOUND
    assert _ask("Welchen Luftwechsel braucht die Tiefgarage vom Bürogebäude Hafenplatz?", office, garage) == NOT_FOUND
    assert "2,10 m" in _ask("Wie hoch ist die Einfahrt der Tiefgarage Rathausplatz?", office, garage)


def test_courtesy_phrases_are_never_quoted():
    reply = ("[1] Mail vom 25.09.2023 (Murat Yilmaz): AW: BS-2099-0002 Bürogebäude Hafenplatz 3: Frage zum Brandschutz"
             + P2, "Betreff: AW: Frage zum Brandschutz\nVon: Murat Yilmaz <my@example.org>\nHallo Paul,\n"
                   "danke für die Nachfrage. Ich sehe mir das bis Ende der Woche an und melde mich mit einer "
                   "Stellungnahme.\nViele Grüße\nMurat Yilmaz")
    concept = ("[2] 02 Konzept/Brandschutzkonzept.docx" + P1,
               "# Brandschutzkonzept\nVerfasser: Ingenieurbüro Feuerfest, Murat Yilmaz.")
    a = _ask("An welchen Projekten arbeitet Murat Yilmaz?", reply, concept)
    assert "Danke für die Nachfrage" not in a and "melde mich" not in a, a
    assert "BS-2099-0002" in a and "BS-2099-0001" in a  # the mail still shows he works on it (subject line)
    assert _ask("Was hat Murat Yilmaz geantwortet?", reply) == NOT_FOUND


def test_overview_phrasings_and_filler_words():
    """"Kurze Zusammenfassung bitte" was answered via "kurz" and "bitte" with "bitte um kurze Rückmeldung"."""
    draft = ("[5] Mail vom 27.10.2024 (Clara Meier): Fluchtbalkon Entwurf" + P1,
             "Guten Tag,\nanbei der Entwurf für den Fluchtbalkon, bitte um kurze Rückmeldung.\nGruß\nClara Meier")
    for q in ("Kurze Zusammenfassung bitte", "Fasse das Projekt kurz zusammen.", "Gib mir eine kurze Übersicht.",
              "Gib mir einen Überblick über das Projekt.", "Was ist das für ein Projekt?", "Was wird hier gebaut?",
              "Erzähl mir was über das Projekt", "Kurz zusammengefasst: was wird gebaut?",
              "Worum geht es bei der Kita Sonnenblume?"):
        a = _ask(q, CONCEPT, draft)
        assert a.startswith("Laut Brandschutzkonzept: Neubau einer fünfgruppigen Kindertagesstätte"), (q, a)


def test_signature_answers_who_signed_and_is_never_a_statement():
    statement = ("[1] Stellungnahme Feuerwehr.pdf (Anhang einer Mail vom 17.06.2024), S. 1" + P1, (
        "Stellungnahme der Brandschutzdienststelle\nBauvorhaben: Kita Sonnenblume, Weg 1, 31777 Musterstadt\n"
        "Die Saugstelle am See ist ganzjährig zugänglich zu halten und zu beschildern.\n"
        "Gegen das Bauvorhaben bestehen bei Beachtung dieser Hinweise keine Bedenken.\n"
        "Stadtbrandmeister Holger Lamm"))
    assert "Holger Lamm" in _ask("Wer hat die Stellungnahme der Feuerwehr unterschrieben?", statement)
    said = _ask("Was hat die Feuerwehr gesagt?", statement)
    assert said != NOT_FOUND and "Holger Lamm" not in said


def test_signature_in_the_last_line_is_not_quoted():
    owner = ("[1] Mail vom 13.05.2025 (Benedikt Sauer): Parkallee 50 – Bäume" + P1, (
        "Guten Tag,\ndie Mitglieder unserer Genossenschaft möchten die Bäume vor dem Hochhaus behalten. Können wir "
        "die Aufstellfläche für die Feuerwehr verschieben? Benedikt Sauer, Wohnbau Stadtpark eG"))
    roles = ("[2] 04 Protokolle/Besprechungsprotokoll Nr. 3.txt" + P1,
             "Besprechungsprotokoll Nr. 3\nTeilnehmer: Benedikt Sauer (Bauherr), Clara Meier (Architektur)")
    a = _ask("Was gibt es Neues vom Bauherrn?", owner, roles)
    assert "Bäume" in a and "Wohnbau Stadtpark eG" not in a, a


def test_role_lines_and_premises_are_no_answer():
    """"Was wollte der Bauherr wissen?" was answered with "Bauherr: …, vertreten durch …"; "Reicht ein
    Vorraum von 5,5 m²?" with the premise of the question mail instead of the Prüfbericht."""
    permit = ("[1] 01 Genehmigung/Baugenehmigung.pdf, S. 1" + P1,
              "Baugenehmigung\nBauherr: Wohnbau Stadtpark eG, vertreten durch Benedikt Sauer\n")
    assert _ask("Was wollte der Bauherr wissen?", permit) == NOT_FOUND
    assert "Benedikt Sauer" in _ask("Wer ist der Bauherr?", permit)
    asking = ("[2] Mail vom 17.11.2024 (Clara Meier): Frage zum Brandschutz" + P1,
              "Hallo Murat,\nDer Vorraum des Feuerwehraufzugs ist in den oberen Geschossen nur 5,5 m² groß. Reicht das?"
              "\nClara Meier")
    report = ("[3] 03 Prüfung/Prüfbericht Nr. 1.md (Stand 12.02.2025)" + P1,
              "## Feststellungen\n1. Der Vorraum des Feuerwehraufzugs muss in allen Geschossen mindestens 6 m² groß sein.")
    a = _ask("Reicht ein Vorraum von 5,5 m² für den Feuerwehraufzug?", asking, report)
    assert "6 m²" in a and "5,5" not in a, a
    asked = _ask("Was hat Clara Meier gefragt?", asking, report)
    assert "Reicht das?" in asked and "5,5 m²" in asked


def test_who_question_for_a_named_person_needs_a_name():
    assert _ask("Welcher Prüfsachverständige nimmt die Brandmeldeanlage ab?", MINUTES) == NOT_FOUND


def test_pronoun_takes_its_antecedent_and_columns_and_spaces_are_clean():
    a = _ask("Wie groß ist der Löschwasserteich?", CONCEPT)
    assert "200 m³" in a and "Löschwasserbedarf" in a  # "Er wird … gedeckt" needs the sentence before
    permit = ("[1] 01 Genehmigung/Baugenehmigung.pdf, S. 1" + P1,
              "Bauaufsicht Musterstadt\nAktenzeichen: BO-233/24    Ihr Zeichen: BS-2099-0001\nBaugenehmigung")
    a = _ask("Welches Aktenzeichen hat die Baugenehmigung?", permit)
    assert "BO-233/24" in a and "  " not in a and "Ihr Zeichen" not in a, a


def test_short_data_lines_and_pieces_are_read():
    invoice = ("[1] Rechnung_2024-1187.pdf (Anhang einer Mail vom 03.12.2024), S. 1",
               "Rechnung Nr. 2024-1187\nToner schwarz, 2 Stück\nGesamtbetrag 312,40 Euro")
    assert "312,40 Euro" in _ask("Wie hoch ist der Gesamtbetrag der Rechnung 2024-1187?", invoice)
    lv = ("[2] BS-2099-0001_LV_Rauchwarnmelder.txt (Anhang einer Mail vom 26.06.2025)" + P1,
          "Leistungsverzeichnis Rauchwarnmelder\nPosition 1: Funkvernetzter Rauchwarnmelder nach DIN 14676, Stück 24.")
    assert "Stück 24" in _ask("Wie viele Rauchwarnmelder sind ausgeschrieben?", lv)
    extinguisher = ("[3] 01 Genehmigung/Baugenehmigung.pdf, S. 2" + P1,
                    "Auflage 9 (Feuerlöscher): Feuerlöscher sind nach ASR A2.2 in ausreichender Anzahl "
                    "bereitzustellen und alle zwei Jahre zu prüfen.")
    assert _ask("Wie viele Feuerlöscher braucht die Kita?", extinguisher) == NOT_FOUND  # "zwei Jahre" is no count


def test_synonyms_file_reaches_reranker_and_reader(tmp_path, monkeypatch):
    path = tmp_path / "synonyme.txt"
    path.write_text("Brandmauer = Brandwand\nKnöpfchen = Handtaster\n", encoding="utf-8")
    doc = "Die Brandwand ist mindestens 0,5 m über Dach zu führen."
    plain = rerank_score("Brandmauer über Dach", doc)
    assert rerank_score("Brandmauer über Dach", doc, active_synonyms(str(path))) > plain
    wall = ("[1] 03 Prüfung/Prüfbericht Nr. 2.md" + P1, "## Feststellungen\n1. " + doc)
    assert _ask("Wie weit muss die Brandmauer über Dach geführt werden?", wall) == NOT_FOUND
    assert "0,5 m" in _ask("Wie weit muss die Brandmauer über Dach geführt werden?", wall,
                           synonyms=active_synonyms(str(path)))
    monkeypatch.setenv("SYNONYMS_FILE", str(path))  # what AI_MODE=fake and the app share
    assert rerank_score("Brandmauer über Dach", doc) > plain
    assert "0,5 m" in _ask("Wie weit muss die Brandmauer über Dach geführt werden?", wall)


def test_synonyms_file_reaches_the_fake_server(tmp_path, monkeypatch, fresh_fake_ai):
    path = tmp_path / "synonyme.txt"
    path.write_text("Brandmauer = Brandwand\n", encoding="utf-8")

    def rerank():
        body = json.dumps({"query": "Brandmauer über Dach", "texts": ["Die Brandwand ist 0,5 m über Dach zu führen."]})
        req = urllib.request.Request(fresh_fake_ai.base_url + "/rerank", data=body.encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)[0]["score"]

    before = rerank()
    monkeypatch.setenv("SYNONYMS_FILE", str(path))
    assert rerank() > before
    monkeypatch.delenv("SYNONYMS_FILE")
    fresh_fake_ai.options["synonyms_file"] = str(path)  # python -m projektakte.fake_ai --synonyms-file
    assert rerank() > before


def test_numbers_alone_are_no_topic_for_the_reranker():
    doc = "Prüfbericht Nr. 1 – Brandmeldeanlage Kategorie 1, Marktplatz 1"
    assert rerank_score("<script>alert(1)</script>", doc) == 0.0
    assert rerank_score("1", doc) == 0.0
    assert rerank_score("xyzzy 1", doc) == 0.0
    assert rerank_score("Kategorie 1", doc) > 0.9
    assert rerank_score("Auflage 7", "Auflage 7 (Rettungswege): Ein zweiter Rettungsweg ist herzustellen.") > 0.9
