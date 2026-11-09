"""Round 7 of the verification (package "oberflaeche"): marks of compound words.

* vor-/nach- before a head that has a prefix of its own are a modifier: "Bescheid" marks
  "Vorbescheid" and "Bauvorbescheid", "Abnahme" "Vorabnahme", "Begehung" "Nachbegehung" – also in
  the snippet window. Before a head without a prefix they make the noun of a verb (Vorschrift,
  Feststellvorrichtung, Nachprüfung): not marked.
* "-her" after a, e or i is the "-er" of an agent noun (Erzieher, Fernseher, Rasenmäher): its
  compounds are marked for their heads again, while "Aufzugshersteller" still marks no "Steller".
* Verb forms keep their particle: "prüft" marks no "überprüft", "trägt" no "überträgt", "gestellt"
  no "festgestellt" – like "Prüfung" marks no "Überprüfung".
* A compound counts only in a form of the query word, not by a stem shared otherwise: "trägt" marks
  no "Brandschutzbeauftragte", "ständig" no "Prüfsachverständige", "Schüler" no "Grundschule" – while
  "Genehmigung" still marks "Baugenehmigungen" and "Räume" now "Treppenräumen".
* Word classes: a word of the query typed with a capital is a noun, one typed in lower case in a
  question that capitalises its nouns is none; where the query does not tell, the text word does. So
  a noun of the query marks compounds in lower-case file names, but its other forms end no word in
  lower case ("Ebene" marks no "angegebenen").

Policy: a missing mark costs little, a wrong one misleads – where the letters leave it open, nothing
is marked. The trade-offs that follow from it are pinned at the end.
"""
from __future__ import annotations

import re

import pytest

from projektakte import web
from projektakte.webtext import QueryMatcher, highlight, snippet

from .test_oberflaeche_r6 import FILLER, _indexed_demo


def _marks(html) -> list[str]:
    return re.findall(r"<mark>(.*?)</mark>", str(html))


def _marked(query: str, word: str) -> bool:
    """Does ``query`` mark ``word`` in a sentence (the word as written, so its case counts)?"""
    hit = QueryMatcher(query).word_matches(word)
    assert _marks(highlight(f"Notiz: {word}.", query)) == ([word] if hit else [])
    return hit


# --------------------------------------------------------------------------- vor-/nach-
@pytest.mark.parametrize("query, word", [
    ("Bescheid", "Vorbescheid"), ("Bescheid", "Bauvorbescheid"), ("Abnahme", "Vorabnahme"),
    ("Abnahme", "Nachabnahme"), ("Begehung", "Vorbegehung"), ("Begehung", "Nachbegehung"),
    ("Entwurf", "Vorentwurf"), ("Besprechung", "Vorbesprechung"), ("Gespräch", "Nachgespräch"),
    ("Anfrage", "Bauvoranfrage"), ("Bericht", "Vorbericht"),
    # the other way round: the query names the compound, the text its head
    ("Vorabnahme", "Abnahme"), ("Nachbegehung", "Begehung"), ("Bauvorbescheid", "Bescheid"),
])
def test_vor_and_nach_before_a_prefixed_head_are_a_modifier(query, word):
    assert _marked(query, word)


@pytest.mark.parametrize("query, word", [
    # the noun of a verb with vor/nach (vorschreiben, feststellen + vorrichten, nachprüfen, vorplanen)
    ("Schrift", "Brandschutzvorschrift"), ("Richtung", "Feststellvorrichtung"), ("Prüfung", "Nachprüfung"),
    ("Prüfung", "Vorprüfung"), ("Planung", "Vorplanung"), ("Frage", "Nachfrage"), ("Stand", "Vorstand"),
    # a chain of prefixes belongs to the head: Bau-Vor-Anfrage, Kosten-Vor-Anschlag
    ("Frage", "Bauvoranfrage"), ("Schlag", "Kostenvoranschlag"),
    # zu/bei with a verb's noun: zuleiten, beiordnen
    ("Leitung", "Zuleitung"), ("Ordnung", "Beiordnung"),
])
def test_vor_and_nach_before_an_unprefixed_head_make_a_derivation(query, word):
    assert not _marked(query, word)


@pytest.mark.parametrize("passage_text, question, word", [
    ("Die Vorabnahme der Brandmeldeanlage erfolgte am 12.03.2024 durch den Sachverständigen. ",
     "Wann war die Abnahme?", "Vorabnahme"),
    ("Die Nachbegehung durch die Feuerwehr ist für den 05.06.2024 angesetzt. ", "Wann ist die Begehung?",
     "Nachbegehung"),
    ("Der Bauvorbescheid wurde am 01.02.2023 vom Bauordnungsamt erteilt. ", "Liegt der Bescheid vor?",
     "Bauvorbescheid"),
])
def test_the_snippet_shows_the_vor_or_nach_compound(passage_text, question, word):
    """verify6: the snippet started at the beginning of the chunk instead of at the compound."""
    text = FILLER + passage_text + FILLER
    s = snippet(text, question)
    assert word in s and s.startswith("…"), s
    assert word in _marks(highlight(s, question))


# --------------------------------------------------------------------------- agent nouns in -her
@pytest.mark.parametrize("query, word", [
    ("Zimmer", "Erzieherzimmer"), ("Personal", "Erzieherpersonal"), ("Wartung", "Rasenmäherwartung"),
    ("Halterung", "Fernseherhalterung"), ("Zimmer", "Aufseherzimmer"),
    ("Wartung", "Feuerlöscherwartung"), ("Erzieherpersonal", "Personal"),
])
def test_an_agent_noun_in_her_is_a_modifier(query, word):
    assert _marked(query, word)


@pytest.mark.parametrize("query, word", [
    ("Steller", "Aufzugshersteller"), ("Stelle", "Aufzugshersteller"),  # Aufzugs-Hersteller
    ("Kennung", "Brandfrüherkennung"), ("Brandfrüherkennung", "Kennung"),  # Brandfrüh-Erkennung
])
def test_her_after_a_consonant_or_u_belongs_to_the_head(query, word):
    assert not _marked(query, word)


# --------------------------------------------------------------------------- verb forms
@pytest.mark.parametrize("question, text", [
    ("Wer prüft die Anlage?", "Die Anlage wird jährlich überprüft."),
    ("Wer trägt die Kosten?", "Der Betreiber überträgt die Aufgabe."),
    ("Wo liegt der Sammelplatz?", "Die Anlage unterliegt der Prüfpflicht."),
    ("Was wurde gelegt?", "Die Fristen wurden festgelegt und die Pläne vorgelegt."),
    ("Wurde der Antrag gestellt?", "Es wurde festgestellt, sichergestellt und fertiggestellt."),
    ("Was wurde gegeben?", "Der Plan wurde freigegeben und weitergegeben."),
])
def test_a_verb_form_keeps_its_particle(question, text):
    """verify6: 'prüft' marked 'überprüft' and 'trägt' 'überträgt' – while 'Überprüfung' was rightly
    not marked for 'Prüfung'. The particle (or what stands before the participle's ge-) is the verb's."""
    query_words = {"Anlage", "Antrag", "Plan"}
    assert [m for m in _marks(highlight(text, question)) if m not in query_words] == []


def test_nouns_of_place_are_still_marked_but_not_their_verbs():
    text = "Das Untergeschoss und die Unterdecke werden überprüft; die Überprüfung ist jährlich."
    assert _marks(highlight(text, "Geschoss Decke prüft Prüfung")) == ["Untergeschoss", "Unterdecke"]
    # the query tells the word class where the text does not: a verb at the start of a sentence
    assert _marks(highlight("Überprüft wird jährlich.", "Wer prüft die Anlage?")) == []


def test_the_class_of_a_query_noun_holds_in_lower_case_file_names():
    """File and folder names are often written in lower case: a noun of the query still marks the
    compounds in them."""
    label = "01_genehmigung/baugenehmigung_2024.pdf, untergeschoss_grundriss.pdf, vorbescheid.pdf"
    assert _marks(highlight(label, "Wo sind die Genehmigung, das Geschoss und der Bescheid?")) == [
        "genehmigung", "baugenehmigung", "untergeschoss", "vorbescheid"]


# --------------------------------------------------------------------------- forms, not stems
@pytest.mark.parametrize("question, text, word", [
    ("Wer trägt die Kosten?", "Die Brandschutzbeauftragte ist Frau Meier.", "Brandschutzbeauftragte"),
    ("Wer trägt die Kosten?", "Der Brandschutzbeauftragte ist Herr Kranz.", "Brandschutzbeauftragte"),
    ("Ist die Anlage ständig besetzt?", "Der Prüfsachverständige hat geprüft.", "Prüfsachverständige"),
    ("Gibt es eine ständige Wache?", "Die Prüfsachverständigen haben geprüft.", "Prüfsachverständigen"),
    ("Wann kommen die Schüler?", "Die Grundschule hat 300 Kinder.", "Grundschule"),
    ("Wo ist die Schule?", "Die Grundschüler sammeln sich im Hof.", "Grundschüler"),
    ("Wie lange muss die Tür halten?", "Das Brandverhalten der Baustoffe.", "Brandverhalten"),
    ("Wo kann die Feuerwehr fahren?", "Das Genehmigungsverfahren läuft.", "Genehmigungsverfahren"),
    ("Wie ist der Stand?", "Der Sachverstand des Prüfers.", "Sachverstand"),
])
def test_a_shared_stem_is_no_compound(question, text, word):
    """verify6: the stem-based head match marked verbs and adjectives inside unrelated nouns."""
    assert word not in _marks(highlight(text, question))


@pytest.mark.parametrize("query, word", [
    # round 6, kept: the text word in another case or number
    ("Genehmigung", "Baugenehmigungen"), ("Geschoss", "Obergeschossen"), ("Leitung", "Steigleitungen"),
    ("Anlage", "Brandmeldeanlagen"), ("Hersteller", "Aufzugsherstellers"),
    # the query word in the plural, the text word in the singular
    ("Anlagen", "Brandmeldeanlage"), ("Genehmigungen", "Baugenehmigung"), ("Aufzüge", "Feuerwehraufzugs"),
    # endings the stem did not cover
    ("Räume", "Treppenräumen"), ("Wände", "Brandwänden"), ("Melder", "Rauchmeldern"),
    # an adjective or participle as a noun: der Beauftragte, ein Beauftragter
    ("Beauftragte", "Brandschutzbeauftragter"), ("Sachverständige", "Prüfsachverständiger"),
    # adjectives
    ("dicht", "rauchdichte"), ("feuerhemmend", "hochfeuerhemmende"),
])
def test_a_compound_in_another_form_of_the_query_word(query, word):
    assert _marked(query, word)


def test_a_noun_of_the_query_in_another_form_ends_nouns_only():
    """The head decides the word class: 'Ebenen' in 'angegebenen' and 'stellen' in 'sicherstellen'
    are no forms of the nouns 'Ebene' and 'Stelle' – the endings of a noun count in a noun only."""
    text = "Die angegebenen Ebenen und die Zugangsebenen; wir sicherstellen die Baustellen."
    assert _marks(highlight(text, "Welche Ebene?")) == ["Ebenen", "Zugangsebenen"]
    assert _marks(highlight(text, "Welche Stelle?")) == ["Baustellen"]


@pytest.fixture(scope="module")
def demo(tmp_path_factory, fake_ai):
    return _indexed_demo(tmp_path_factory.mktemp("r7"), fake_ai)


@pytest.mark.parametrize("question, word", [
    ("Ist die Anlage ständig besetzt?", "Prüfsachverständig"), ("Wer trägt die Kosten?", "Brandschutzbeauftragt"),
])
def test_the_pages_do_not_mark_a_shared_stem(demo, question, word):
    """verify6 on the demo data: the sources of 'Ist die Anlage ständig besetzt?' marked
    'Prüfsachverständige'."""
    for page in (demo.post("/fragen", data={"question": question}).text,
                 demo.get("/suche", params={"q": question}).text):
        assert not [m for m in _marks(page) if m.startswith(word)], question


def test_the_answer_page_marks_the_compounds_of_the_question(demo, monkeypatch):
    text = ("Der Vorbescheid und die Nachbegehung: In den Gruppenräumen trägt der Betreiber die Kosten; "
            "die Brandschutzbeauftragte überträgt die Aufgabe.")
    src = {"n": 1, "kind": "doc", "doc_id": 1, "chunk_id": 1, "label": "Protokoll", "project": None,
           "snippet": text, "text": text}

    def answer(conn, ai, path, question, **kw):
        return {"status": "answered", "answer": "Siehe Protokoll [1].", "notes": [], "question": question,
                "project": None, "sources": [src], "context": [src]}
    monkeypatch.setattr(web, "answer_question", answer)
    question = "Wer trägt die Kosten der Räume, liegt der Bescheid vor und wann ist die Begehung?"
    for headers in ({}, {"HX-Request": "true"}):
        page = demo.post("/fragen", data={"question": question}, headers=headers).text
        quote = re.search(r"<blockquote>(.*?)</blockquote>", page, re.S).group(1)
        assert _marks(quote) == ["Vorbescheid", "Nachbegehung", "Gruppenräumen", "trägt", "Kosten"], quote


# --------------------------------------------------------------------------- trade-offs
@pytest.mark.parametrize("query, word", [
    # "-ver" at the end of a modifier is the prefix ver-: the few nouns in -ver lose their compounds
    ("Löscher", "Pulverlöscher"), ("Wartung", "Serverwartung"),
    # an adjective compound with a participle as head reads like the participle of a verb
    ("gesteuert", "brandfallgesteuert"),
    # Stab- ends like the prefix ab- (Rauch-Ableitung, Last-Abtragung)
    ("Stahl", "Betonstabstahl"),
])
def test_documented_trade_offs_are_not_marked(query, word):
    assert not _marked(query, word)
