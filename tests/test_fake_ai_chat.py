"""The fake chat behaves like a sensible extractive model (not like a lookup table for the
evaluation set): readable "Laut …" answers, only relevant sentences, no headings/tables/greetings,
"Nicht gefunden." when the topic is missing – even if the question names the project."""
from projektakte.fake_ai.core import NOT_FOUND, answer_from_sources, chat_completion, parse_sources, rerank_score

PROJ = " – Projekt BS-2099-0001 Hotel Morgenrot"
PERMIT = ("[1] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 02.07.2024)" + PROJ, (
    "Auflagen zum Brandschutz – Hotel Morgenrot\n"
    "Auflage 3 (Brandmeldeanlage): Die Gästezimmer erhalten Rauchmelder mit Aufschaltung auf die Brandmeldeanlage.\n"
    "Auflage 4 (Sicherheitsbeleuchtung): In allen Fluren ist eine Sicherheitsbeleuchtung mit einer Betriebsdauer "
    "von mindestens 8 Stunden vorzusehen."))
PERMIT_NEXT = ("[2] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 02.07.2024)" + PROJ, (
    "Auflage 4 (Sicherheitsbeleuchtung): In allen Fluren ist eine Sicherheitsbeleuchtung mit einer Betriebsdauer "
    "von mindestens 8 Stunden vorzusehen.\n"
    "Auflage 5 (Feuerlöscher): Feuerlöscher sind alle zwei Jahre durch einen Sachkundigen zu prüfen."))
REPORT = ("[3] 03 Prüfung/Prüfbericht Nr. 1.md (Stand 01.10.2024)" + PROJ, (
    "# Prüfbericht Nr. 1 zum Brandschutznachweis\n| Angabe | Inhalt |\n|---|---|\n"
    "| Projekt | BS-2099-0001 Hotel Morgenrot |\n| Prüfingenieur | Dipl.-Ing. Eva Muster, Prüfbüro Muster |\n"
    "## Feststellungen\n1. Die Länge des Stichflurs im 3. Obergeschoss überschreitet das zulässige Maß von 15 m."))
OLD_MAIL = ("[4] Mail vom 01.09.2024 (Karl Bauer): BS-2099-0001 – Zwischenstand" + PROJ, (
    "Betreff: BS-2099-0001 – Zwischenstand\nVon: Karl Bauer <k@bauer.example>\nDatum: 2024-09-01T08:00:00+00:00\n"
    "Guten Tag,\ndie Ausschreibung der Brandmeldeanlage läuft noch.\nMit freundlichen Grüßen\nKarl Bauer\nBauer GmbH"))
NEW_MAIL = ("[5] Mail vom 05.11.2024 (Karl Bauer): BS-2099-0001 – Stand" + PROJ, (
    "Betreff: BS-2099-0001 – Stand\nVon: Karl Bauer <k@bauer.example>\nDatum: 2024-11-05T08:00:00+00:00\n"
    "Hallo Frau Muster,\nder Auftrag für die Brandmeldeanlage ist vergeben. Muss die Steuermatrix vorher vorliegen?\n"
    "Viele Grüße\nKarl Bauer"))
MINUTES = ("[6] 04 Protokolle/2024-10-10 Besprechungsprotokoll Nr. 2.txt (Stand 10.10.2024)" + PROJ, (
    "Besprechungsprotokoll Nr. 2\nTeilnehmer: Karl Bauer (Bauherr), Eva Muster (Prüfung)\nTOP 1 – Brandmeldeanlage\n"
    "Die Ausschreibung wird bis Ende Oktober abgeschlossen."))


def _ask(question, *sources):
    raw = [(int(h[1]), h.split("] ", 1)[1], t) for h, t in sources]
    return answer_from_sources(question, raw)


def test_answers_with_the_relevant_sentence_once():
    a = _ask("Wie lange muss das Notlicht funktionieren?", PERMIT, PERMIT_NEXT, REPORT)
    assert a.startswith("Laut Baugenehmigung (S. 2), Auflage 4 (Sicherheitsbeleuchtung): In allen Fluren")
    assert a.count("8 Stunden") == 1 and "Rauchmelder" not in a and "Feuerlöscher" not in a
    assert a.rstrip().endswith(("[1]", "[2]"))


def test_abbreviation_and_question_type():
    assert "Rauchmelder" in _ask("Welche Auflage gibt es zur BMA?", PERMIT, REPORT)
    a = _ask("Wie oft müssen die Feuerlöscher geprüft werden?", PERMIT, PERMIT_NEXT)
    assert "alle zwei Jahre" in a and "[2]" in a


def test_project_name_is_no_answer():
    assert _ask("Wie viele Fahrradstellplätze hat das Hotel Morgenrot?", PERMIT, REPORT, MINUTES) == NOT_FOUND
    assert _ask("Welche Sprinkleranlage ist für BS-2099-0001 vorgesehen?", PERMIT, REPORT) == NOT_FOUND
    assert _ask("Was ist mit dem Hotel Morgenrot?", PERMIT, REPORT) == NOT_FOUND


def test_never_quotes_headings_tables_greetings_or_questions():
    a = _ask("Wer ist der Prüfingenieur?", REPORT, PERMIT)
    assert "|" not in a and "#" not in a and "Dipl.-Ing. Eva Muster" in a
    a = _ask("Was ist mit der Brandmeldeanlage passiert?", NEW_MAIL, OLD_MAIL)
    assert "Hallo" not in a and "Grüße" not in a and "?" not in a and "Steuermatrix" not in a
    assert "Laut Mail von Karl Bauer vom 05.11.2024" in a or "Laut Mail von Karl Bauer vom 01.09.2024" in a


def test_latest_state_uses_the_newest_source_and_resolves_roles():
    a = _ask("Was ist der letzte Stand mit dem Bauherrn?", MINUTES, OLD_MAIL, NEW_MAIL, PERMIT)
    assert a.startswith("Laut Mail von Karl Bauer vom 05.11.2024: Der Auftrag für die Brandmeldeanlage ist vergeben.")
    a = _ask("Ist die Ausschreibung der Brandmeldeanlage inzwischen abgeschlossen?", OLD_MAIL, MINUTES)
    assert "Ende Oktober" in a  # newer of the two matching sources
    assert "Teilnehmer" not in _ask("Gibt es Neuigkeiten vom Bauherrn?", MINUTES, NEW_MAIL)


def test_several_projects_are_named_and_listed():
    other = ("[7] 01 Genehmigung/Baugenehmigung.pdf, S. 2 (Stand 01.01.2024) – Projekt BS-2099-0002 Schule Nord",
             "Auflage 2 (Brandmeldeanlage): Die Schule erhält eine Brandmeldeanlage der Kategorie 2.")
    a = _ask("In welchen Projekten ist eine Brandmeldeanlage gefordert?", PERMIT, other)
    assert a.startswith("Gefunden in 2 Projekten:") and "BS-2099-0001 Hotel Morgenrot – laut" in a
    assert "BS-2099-0002 Schule Nord – laut" in a
    a = _ask("Welche Kategorie hat die Brandmeldeanlage der Schule Nord?", PERMIT, other)
    assert "Kategorie 2" in a and "[7]" in a and "[1]" not in a


def test_chat_completion_format_and_determinism():
    prompt = "Quellen:\n\n" + "\n\n".join(f"{h}\n{t}" for h, t in (PERMIT, REPORT)) + "\n\nFrage: Wie lange hält das Notlicht?"
    q, sources = parse_sources(prompt)
    assert q == "Wie lange hält das Notlicht?" and [n for n, _, _ in sources] == [1, 3]
    payload = {"model": "m", "messages": [{"role": "user", "content": prompt}]}
    a, b = chat_completion(payload), chat_completion(payload)
    assert a == b and a["choices"][0]["finish_reason"] == "stop"
    assert a["choices"][0]["message"]["content"].startswith("<think>")


def test_reranker_ignores_filler_words_and_knows_synonyms():
    q = "In welchen Projekten ist eine Sprachalarmanlage vorgesehen?"
    filler = "Projekt: BS-2024-0058 Bürogebäude. Die Abnahme der Druckbelüftungsanlage ist für März vorgesehen."
    topic = "Die Alarmierung erfolgt über eine Sprachalarmanlage."
    assert rerank_score(q, topic) > 0.9 > 0.1 > rerank_score(q, filler)
    assert rerank_score("Wann wird die BMA abgenommen?",
                        "Die Abnahme der Brandmeldeanlage ist für den 18.06.2025 geplant.") > 0.9
    assert rerank_score("Was fordert Auflage 7 für BS-2024-0137?", "Projekt BS-2024-0137 Grundschule") == 0.0
