"""Typing errors in search queries (tippfehler.py): a word that finds (nearly) nothing is also searched as
the closest frequent word of the index; correct words are left alone; fast with a large vocabulary."""
import random
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from projektakte import db, tippfehler as tf
from projektakte.ai.services import AIServices
from projektakte.dokumentarten import osa_distance, typo_limit
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.search import hybrid_search

from .conftest import fake_settings

PROJECT = "BS-2032-0001"
TEXTS = {
    "Konzept.txt": "Brandschutzkonzept für die Sporthalle\n\nDer Rettungsweg führt über den Flur ins Freie. "
                   "Die Feuerwehrzufahrt liegt an der Nordseite.",
    "Bericht.txt": "Prüfbericht zum Brandschutzkonzept\n\nDer zweite Rettungsweg ist über Leitern der Feuerwehr "
                   "gesichert. Die Feuerwehrzufahrt ist ausreichend breit.",
    "Protokoll.txt": "Besprechungsprotokoll\n\nDer Rettungsweg im Obergeschoss wird verbreitert. Rauchschutztür "
                     "im Treppenraum. Die Rauchschutztür schließt selbsttätig.",
    "Notiz.txt": "Notiz\n\nDie Sprinklerzentrale ist im Keller hinter der Rauchschutztür. Ein Wort kommt nur "
                 "einmal vor: Kellerassel.",
}


@pytest.fixture(scope="module")
def env(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("tippfehler")
    folder = tmp / "projekte" / f"{PROJECT} Sporthalle Am Bach"
    folder.mkdir(parents=True)
    for name, text in TEXTS.items():
        (folder / name).write_text(text, encoding="utf-8")
    s = fake_settings(fake_ai, tmp, PROJECTS_DIR=str(tmp / "projekte"))
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    tf.VOCABULARY.clear()
    return s, ai, conn


def test_edit_distance():
    assert osa_distance("brandschutzkozept", "brandschutzkonzept") == 1
    assert osa_distance("branschutzkonzept", "brandschutzkonzept") == 1
    assert osa_distance("rettungswge", "rettungswege") == 1
    assert osa_distance("rettunsgweg", "rettungsweg") == 1  # swapped neighbours: one edit
    assert osa_distance("abcdef", "uvwxyz", limit=2) == 3  # cut off above the limit
    assert [typo_limit(w) for w in ("tuer", "rauchs", "rettungsw", "brandschutzkonzept")] == [0, 1, 2, 2]


@pytest.mark.parametrize("typed, expected", [
    ("Brandschutzkozept", "Brandschutzkonzept"),
    ("Branschutzkonzept", "Brandschutzkonzept"),
    ("Rettungswek", "Rettungsweg"),
    ("Feuerwehrzufart", "Feuerwehrzufahrt"),
    ("Rauchschutztüür", "Rauchschutztür"),
])
def test_misspelt_words_are_corrected_to_words_of_the_index(env, typed, expected):
    _, _, conn = env
    found = tf.corrections(conn, typed)
    assert [(c.typed, c.word, c.unknown) for c in found] == [(typed, expected, True)]


@pytest.mark.parametrize("query", ["Rettungsweg", "Brandschutzkonzepte", "Kellerassel", "Tür T30",
                                   "BS-2032-0001", "Sprinklerzentrale", "Photovoltaikanlage", "Welche Auflagen gibt es"])
def test_correct_rare_or_unknown_words_are_left_alone(env, query):
    """Found words, words with digits and short ones are never corrected; an unknown word without a close
    neighbour stays as typed (nothing is invented)."""
    _, _, conn = env
    assert tf.corrections(conn, query) == []


def test_search_finds_the_document_despite_the_typo(env):
    s, ai, conn = env
    for typed in ("Brandschutzkozept", "Rettungswek Obergeschoss"):
        res = hybrid_search(conn, ai, str(s.db_path), typed, k=5)
        assert res.hits and res.corrections, typed
        assert res.hits[0].title in ("Konzept.txt", "Bericht.txt", "Protokoll.txt")
    res = hybrid_search(conn, ai, str(s.db_path), "Rettungswek Obergeschoss", k=5)
    assert res.hits[0].title == "Protokoll.txt"
    off = hybrid_search(conn, ai, str(s.db_path), "Brandschutzkozept", k=5, typos=False)
    assert not off.corrections


def test_corrected_query_replaces_only_unknown_words():
    found = [tf.Correction("Brandschutzkozept", "Brandschutzkonzept", True), tf.Correction("Halle", "Hallen", False)]
    assert tf.corrected_query("Brandschutzkozept Halle", found) == "Brandschutzkonzept Halle"


def test_search_page_says_what_was_searched(env):
    s, _, _ = env
    with TestClient(create_app(s, background=False)) as client:
        html = client.get("/suche", params={"q": "Feuerwehrzufart"}).text
    assert "„Feuerwehrzufart“ kommt nicht vor – meinten Sie" in html and "„Feuerwehrzufahrt“" in html
    assert "q=Feuerwehrzufahrt" in html  # the link searches the corrected words


def test_vocabulary_is_read_again_after_the_index_changed(tmp_path, monkeypatch):
    p = tmp_path / "v.sqlite3"
    db.init_db(p)
    conn = db.connect(p)

    def add(i, text):
        conn.execute("INSERT INTO chunks(id, ord, text) VALUES(?, 0, ?)", (i, text))
        conn.execute("INSERT INTO chunks_fts(rowid, title, body) VALUES(?, '', ?)", (i, text))
        conn.commit()
    add(1, "sprinkleranlage")
    add(2, "sprinkleranlage")
    tf.VOCABULARY.clear()
    assert tf.VOCABULARY.get(conn).frequency("sprinkleranlage") == 2
    monkeypatch.setattr(tf, "REFRESH_SECONDS", 0.0)
    add(3, "wandhydrant")
    add(4, "wandhydrant")
    assert tf.VOCABULARY.get(conn).frequency("wandhydrant") == 2
    tf.VOCABULARY.clear()


def _synthetic_terms(n: int, seed: int = 7) -> list[tuple[str, int]]:
    parts = ["brand", "schutz", "konzept", "rettung", "weg", "treppen", "raum", "flucht", "plan", "losch",
             "wasser", "anlage", "melder", "tur", "decke", "wand", "bericht", "pruf", "nachweis", "feuer", "wehr",
             "zufahrt", "halle", "schule", "dach", "keller", "luftung", "rauch", "abzug", "alarm", "technik"]
    rnd = random.Random(seed)
    seen: dict[str, int] = {}
    while len(seen) < n:
        w = "".join(rnd.choice(parts) for _ in range(rnd.randint(2, 4))) + rnd.choice(["", "e", "en", "s"])
        if rnd.random() < 0.5:
            w += str(rnd.randint(0, 999))
        seen.setdefault(w, rnd.randint(2, 5000))
    seen["brandschutzkonzept"] = 9000
    return list(seen.items())


def test_large_vocabulary_stays_fast():
    """400.000 words (MAX_TERMS) are built in seconds and a word is looked up in milliseconds."""
    terms = _synthetic_terms(tf.MAX_TERMS)
    started = time.perf_counter()
    vocab = tf.Vocabulary(terms)
    build = time.perf_counter() - started
    started = time.perf_counter()
    for word in ("brandschutzkozept", "branschutzkonzept", "feuerwehrzufart", "rettungswek", "qqqqqqqqqqq"):
        vocab.closest(word)
    lookup = (time.perf_counter() - started) / 5
    assert vocab.closest("brandschutzkozept")[0][:2] == ("brandschutzkonzept", 1)
    assert build < 20 and lookup < 0.25, (build, lookup)


def test_vocabulary_from_a_large_index_keeps_the_frequent_words(tmp_path):
    p = tmp_path / "gross.sqlite3"
    db.init_db(p)
    conn = db.connect(p)
    words = [w for w, _ in _synthetic_terms(30000, seed=3)]
    rnd = random.Random(5)
    conn.executemany("INSERT INTO chunks_fts(rowid, title, body) VALUES(?, '', ?)",
                     [(i, " ".join(rnd.choice(words) for _ in range(40)) + " brandschutzkonzept")
                      for i in range(1, 20001)])
    conn.commit()
    started = time.perf_counter()
    vocab = tf.read_vocabulary(conn, max_terms=5000)
    seconds = time.perf_counter() - started
    assert vocab.size == 5000 and vocab.frequency("brandschutzkonzept") == 20000
    assert seconds < 10, seconds
