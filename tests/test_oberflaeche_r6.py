"""Round 6 of the verification (package "oberflaeche").

* Compound parts: a word is marked as the head of a longer one only where the rest is a word of its
  own – a modifier, never a prefix. Teilabnahme, Probealarmierung, Nutzereinweisung, Weglänge and
  Festverglasung are marked for their heads again (round 5 had lost them, and with them the snippet
  window), while Rauchableitung still does not mark "Leitung" and Brandschutzunterweisung does not mark
  "Weisung". Inflected compounds (Baugenehmigungen, Obergeschossen) count like their base form.
* The configured PROJECT_NUMBER_PATTERN reaches the marks, snippets and passages of the real pages
  (the macros are imported with the page's context).
* A card shown once more from an earlier page keeps its mark when the database is busy.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from projektakte import db, web
from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.webtext import QueryMatcher, highlight, snippet

from .conftest import fake_settings
from .test_oberflaeche_r5 import _cards, _fill_inbox, _link, _post

TEMPLATES = Path(web.__file__).parent / "templates"
NOTE = "Bereits auf einer vorigen Seite gezeigt und dort mitgezählt – noch offen."


def _marks(html) -> list[str]:
    return re.findall(r"<mark>(.*?)</mark>", str(html))


def _indexed_demo(root, fake_ai, **extra) -> TestClient:
    generate(root / "demo")
    s = fake_settings(fake_ai, root, DEMO_MODE=1, **extra)
    run_index(s, AIServices.from_settings(s))
    return TestClient(create_app(s, background=False))


# --------------------------------------------------------------------------- compound parts
# The query names the head, the text word is a compound with a modifier of its own → marked.
HEADS = [
    ("Abnahme", "Teilabnahme"), ("Baugenehmigung", "Teilbaugenehmigung"), ("Sanierung", "Teilsanierung"),
    ("Länge", "Weglänge"), ("Breite", "Wegbreite"), ("Verglasung", "Festverglasung"), ("Fläche", "Freifläche"),
    ("Sperrung", "Vollsperrung"), ("Inbetriebnahme", "Wiederinbetriebnahme"), ("Prüfung", "Gegenprüfung"),
    # modifiers ending like a prefix: Pro-be, Nutz-er, Funda-ment, Gewer-be, Pul-ver
    ("Bohrung", "Probebohrung"), ("Messung", "Probemessung"), ("Alarmierung", "Probealarmierung"),
    ("Auslösung", "Probeauslösung"), ("Schulung", "Nutzerschulung"), ("Einweisung", "Nutzereinweisung"),
    ("Führung", "Benutzerführung"), ("Anmeldung", "Gewerbeanmeldung"), ("Abdichtung", "Fundamentabdichtung"),
    ("Löscher", "Pulverlöscher"), ("Wartung", "Feuerlöscherwartung"), ("Richtlinie", "Hochhausrichtlinie"),
    ("Mauerwerk", "Kalksandsteinmauerwerk"), ("Lüftung", "Treppenraumlüftung"), ("Prüfung", "Planprüfung"),
    ("Hersteller", "Aufzugshersteller"), ("Belüftung", "Treppenhausbelüftung"),
    # prepositions of place make nouns of place
    ("Geschoss", "Untergeschoss"), ("Decke", "Unterdecke"), ("Kante", "Unterkante"),
    ("Verteilung", "Unterverteilung"), ("Druck", "Überdruck"), ("Eingang", "Hintereingang"),
    ("Beanspruchung", "Überbeanspruchung"),
    # the prefix belongs to the head that the query names
    ("Unterweisung", "Brandschutzunterweisung"), ("Ableitung", "Rauchableitung"), ("Zufahrt", "Feuerwehrzufahrt"),
    ("Abschnitt", "Brandabschnitt"), ("Entrauchung", "Treppenraumentrauchung"), ("Übergabe", "Schlüsselübergabe"),
    ("Genehmigung", "Baugenehmigung"), ("Leitung", "Steigleitung"), ("Prüfung", "Abnahmeprüfung"),
    # inflected compounds
    ("Genehmigung", "Baugenehmigungen"), ("Geschoss", "Obergeschossen"), ("Beleuchtung", "Notbeleuchtungen"),
    ("Leitung", "Steigleitungen"), ("Anlage", "Brandmeldeanlagen"),
]
# The query names a word that only ends the text word: the rest is a prefix → not marked.
DERIVATIONS = [
    ("Leitung", "Rauchableitung"), ("Leitung", "Ableitung"), ("Leitung", "Umleitung"),
    ("Weisung", "Brandschutzunterweisung"), ("Weisung", "Unterweisung"), ("Weisung", "Einweisung"),
    ("Brechung", "Unterbrechung"), ("Spruch", "Widerspruch"), ("Stand", "Feuerwiderstand"),
    ("Schnitt", "Brandabschnitt"), ("Schnitt", "Abschnitt"), ("Fahrt", "Zufahrt"), ("Fahrt", "Feuerwehrzufahrt"),
    ("Prüfung", "Überprüfung"), ("Prüfung", "Brandschutzüberprüfung"), ("Lüftung", "Hinterlüftung"),
    ("Wartung", "Erwartung"), ("Stellung", "Herstellung"), ("Stellung", "Bestellung"), ("Lüftung", "Belüftung"),
    ("Ordnung", "Verordnung"), ("Ordnung", "Prüfverordnung"), ("Sicherung", "Gebäudeversicherung"),
    ("Führung", "Durchführung"), ("Führung", "Ausführung"), ("Haltung", "Unterhaltung"),
    ("Bescheid", "Vorbescheid"), ("Bescheid", "Bauvorbescheid"), ("Stelle", "Aufzugshersteller"),
    ("Schnittsbildung", "Brandabschnittsbildung"), ("Zugsanlage", "Rauchabzugsanlage"),
    ("geführt", "ausgeführt"), ("führt", "durchgeführt"), ("findet", "stattfindet"), ("reicht", "nachgereichten"),
    ("trägt", "beantragte"), ("erteilt", "verteilt"), ("Rauch", "brauchen"), ("weiter", "zweiter"),
]
# The query names the compound, the text only its head (or only the end of it).
REVERSE = [
    ("Teilabnahme", "Abnahme", True), ("Teilsanierung", "Sanierung", True), ("Festverglasung", "Verglasung", True),
    ("Probebohrung", "Bohrung", True), ("Probealarmierung", "Alarmierung", True), ("Nutzerschulung", "Schulung", True),
    ("Nutzereinweisung", "Einweisung", True), ("Untergeschoss", "Geschoss", True),
    ("Baubesprechung", "Besprechung", True), ("Rauchableitung", "Ableitung", True),
    ("Brandschutzunterweisung", "Unterweisung", True),
    ("Rauchableitung", "Leitung", False), ("Brandschutzunterweisung", "Weisung", False),
    ("Unterweisung", "Weisung", False),
    ("Widerspruch", "Spruch", False), ("Unterbrechung", "Brechung", False), ("Überprüfung", "Prüfung", False),
    ("Prüfverordnung", "Ordnung", False), ("Brandabschnittsbildung", "Schnittsbildung", False),
    ("Rauchabzugsanlage", "Zugsanlage", False), ("ausgeführt", "geführt", False),
]


@pytest.mark.parametrize("query, word", HEADS)
def test_a_compound_is_marked_for_its_head(query, word):
    assert QueryMatcher(query).word_matches(word), (query, word)
    assert _marks(highlight(f"Die {word} ist erledigt.", query)) == [word]


@pytest.mark.parametrize("query, word", DERIVATIONS)
def test_a_derivation_is_not_marked_for_the_word_it_ends_with(query, word):
    assert not QueryMatcher(query).word_matches(word), (query, word)
    assert _marks(highlight(f"Die {word} ist erledigt.", query)) == []


@pytest.mark.parametrize("query, word, matches", REVERSE)
def test_the_head_of_a_compound_query(query, word, matches):
    assert QueryMatcher(query).word_matches(word) is matches


def test_question_with_a_derived_compound_does_not_mark_the_bare_word():
    """verify5: 'Wann war die Brandschutzunterweisung?' marked 'Weisung' in '… auf Weisung der Feuerwehr'."""
    text = "Die Brandschutzunterweisung fand am 3. März statt. Die Räumung erfolgt auf Weisung der Feuerwehr."
    assert _marks(highlight(text, "Wann war die Brandschutzunterweisung?")) == ["Brandschutzunterweisung"]
    assert _marks(highlight(text, "Welche Unterweisung gab es?")) == ["Brandschutzunterweisung"]


def test_a_verb_particle_alone_is_no_query_word():
    """A question joins the particle to its verb ('findet … statt' → 'stattfindet'); typed alone
    ('Begehung statt') it is no content word either – and never the 'Stätte' of 'Kindertagesstätte'
    (both fold to 'statt'), now that inflected compounds count like their base form."""
    text = "Die Begehung der Kindertagesstätte fand am Montag statt."
    assert _marks(highlight(text, "Wann findet die Begehung statt?")) == ["Begehung"]
    assert _marks(highlight(text, "Begehung statt")) == ["Begehung"]
    assert _marks(highlight("Die Kindertagesstätte am Regenbogenweg.", "Stätte")) == ["Kindertagesstätte"]


FILLER = "Allgemeine Angaben zum Gebäude und zur Nutzung werden im Folgenden beschrieben. " * 12


@pytest.mark.parametrize("passage_text, question, word", [
    ("Die Teilabnahme der Brandmeldeanlage erfolgte am 12.03.2024 durch den Sachverständigen. ",
     "Wann war die Abnahme?", "Teilabnahme"),
    ("Die Probealarmierung wurde am Montag durchgeführt, alle Melder haben ausgelöst. ", "Alarmierung",
     "Probealarmierung"),
    ("Die Nutzereinweisung ist für den 4. Mai geplant. ", "Wann ist die Einweisung?", "Nutzereinweisung"),
    ("Beide Baugenehmigungen liegen seit Juni vor. ", "Liegt die Genehmigung vor?", "Baugenehmigungen"),
])
def test_the_snippet_shows_the_compound_of_the_question_word(passage_text, question, word):
    """verify5: the snippet started at the beginning of the chunk instead of at 'Teilabnahme'."""
    text = FILLER + passage_text + FILLER
    s = snippet(text, question)
    assert word in s and s.startswith("…"), s
    assert word in _marks(highlight(s, question))


def test_the_snippet_does_not_jump_to_a_derivation():
    text = FILLER + "Die Rauchableitung erfolgt über das Fenster im Treppenraum. " + FILLER
    assert snippet(text, "Wer hat die Leitung?") == snippet(text, "")  # nothing to show: the beginning


# --------------------------------------------------------------------------- PROJECT_NUMBER_PATTERN on real pages
def test_every_import_of_the_macros_keeps_the_page_context():
    """The macros' filters read the app's settings from the request; an import without context
    silently falls back to the default number shape (verify5)."""
    imports = []
    for path in TEMPLATES.glob("*.html"):
        for stmt in re.findall(r"{%-?\s*(?:from|import)\s+[\"']_macros\.html[\"'].*?-?%}", path.read_text("utf-8")):
            imports.append((path.name, stmt))
            assert re.search(r"\bwith\s+context\s*-?%}$", stmt), (path.name, stmt)
    assert {name for name, _ in imports} >= {"search.html", "_answer.html"}


@pytest.fixture(scope="module")
def p_app(tmp_path_factory, fake_ai):
    """Demo app whose project numbers have the shape P12345; every chunk names plan P12345."""
    client = _indexed_demo(tmp_path_factory.mktemp("r6p"), fake_ai, PROJECT_NUMBER_PATTERN=r"P\d{5}")
    with db.session(client.app.state.settings.db_path) as conn:
        conn.execute("UPDATE chunks SET text = 'Plan P12345. ' || text")
    return client


def test_search_page_marks_numbers_in_the_configured_shape(p_app):
    assert p_app.app.state.settings.project_number_pattern == r"P\d{5}"
    page = p_app.get("/suche", params={"q": "Brandschutzkonzept P 12345"}).text
    hits = page[page.index('class="hits'):] if 'class="hits' in page else page
    assert "<mark>P12345</mark>" in hits
    assert "P12345" not in re.sub(r"<mark>P12345</mark>", "", hits), "every P12345 of the hits is marked"


def _answer(question: str) -> dict:
    text = "Plan P12345 vom Mai zeigt die Rettungswege."
    src = {"n": 1, "kind": "doc", "doc_id": 1, "chunk_id": 1, "label": "Planliste", "project": None,
           "snippet": text, "text": text}
    return {"status": "answered", "answer": "Plan P12345 zeigt die Rettungswege [1].", "notes": [],
            "question": question, "project": None, "sources": [src], "context": [src]}


@pytest.mark.parametrize("htmx", [False, True])
def test_ask_page_marks_numbers_in_the_configured_shape(p_app, monkeypatch, htmx):
    """The quote of a source (passage() + highlight in the macro source_list) – on the full page and
    in the HTMX answer."""
    monkeypatch.setattr(web, "answer_question", lambda conn, ai, path, question, **kw: _answer(question))
    headers = {"HX-Request": "true"} if htmx else {}
    page = p_app.post("/fragen", data={"question": "Was zeigt Plan P 12345?"}, headers=headers).text
    quote = re.search(r"<blockquote>(.*?)</blockquote>", page, re.S).group(1)
    assert "<mark>P12345</mark>" in quote, quote


def test_default_shape_is_not_marked_for_another_configured_shape(p_app):
    """The other way round: with PROJECT_NUMBER_PATTERN=P\\d{5} a query 'BS 2024 0137' is no project
    number – its parts are not joined into one."""
    page = p_app.get("/suche", params={"q": "BS 2024 0137"}).text
    assert "<mark>BS-2024-0137</mark>" not in page


# --------------------------------------------------------------------------- busy database
@pytest.fixture()
def rw(tmp_path, fake_ai):
    return _indexed_demo(tmp_path, fake_ai)


def _card_classes(html: str, mail_id: int) -> str:
    return re.search(rf'<article class="([^"]*)" id="mail-{mail_id}"', html).group(1)


def test_busy_database_keeps_the_mark_of_a_card_shown_again(rw, monkeypatch):
    """verify5: the HTMX answer for the repeated card lost 'repeated' and its note while the database
    was locked (a failed decision with DecisionError kept it)."""
    _fill_inbox(rw, 10, 60)
    page1 = rw.get("/eingang").text
    ids = [i for i, _ in _cards(page1)]
    _post(rw, ids[0], page1, "reject")
    for mid in ids[1:5]:
        r = _post(rw, mid, page1, "none")
    page2 = rw.get(_link(r, "next")).text
    again = [i for i, rep in _cards(page2) if rep]
    other = next(i for i, rep in _cards(page2) if not rep)
    assert again == [ids[0]]

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(web, "_decide", locked)
    r = _post(rw, ids[0], page2, "assign", project="BS-2024-0137")
    assert _card_classes(r, ids[0]).split() == ["card", "inbox-item", "repeated"] and NOTE in r
    assert web.BUSY_TEXT in r
    assert 'value="BS-2024-0137"' in r  # the typed project stays
    r = _post(rw, other, page2, "none")  # a card counted on this page is not marked by the busy answer
    assert _card_classes(r, other).split() == ["card", "inbox-item"] and NOTE not in r
