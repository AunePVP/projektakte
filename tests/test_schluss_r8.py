"""Last cross-check: a very long word must not make highlighting slow (the compound scan was quadratic)."""
import time

from projektakte.webtext import QueryMatcher


def test_a_long_word_is_highlighted_in_linear_time():
    q = QueryMatcher("Genehmigung Brandschutz Wartung Prüfbericht Rauchableitung")
    word = "Betreff" + "a" * 50_000  # e.g. a mail subject without spaces
    started = time.perf_counter()
    assert not q.word_matches(word)
    q.spans(("Baugenehmigung " * 2000) + word)
    assert time.perf_counter() - started < 0.5
    assert q.word_matches("Baugenehmigungen") and q.word_matches("Teilbaugenehmigung")


# -- the last wrong answers of the fake reader --------------------------------------------------------------
import sqlite3  # noqa: E402

import pytest  # noqa: E402

from projektakte.ai.services import AIServices  # noqa: E402
from projektakte.ask import ask  # noqa: E402
from projektakte.demo import generate  # noqa: E402
from projektakte.indexer import run_index  # noqa: E402



@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    """The demo as the app runs it: DEMO_MODE=1, AI_MODE=fake (fake services in-process, images read)."""
    from projektakte.ai.services import resolve_settings
    from projektakte.config import load_settings

    tmp = tmp_path_factory.mktemp("r8")
    generate(tmp / "demo")
    s = load_settings({"DATA_DIR": str(tmp), "DEMO_MODE": "1", "AI_MODE": "fake", "APP_ENV": "test"})
    run_index(s)
    s = resolve_settings(s)
    ai = AIServices.from_settings(s)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    yield s, ai, conn
    conn.close()


@pytest.mark.parametrize("question,project", [
    # a defect of another component (the defect word, the party and "festgestellt" alone are no topic)
    ("Welche Mängel hat die Prüfingenieurin an der Brandmeldeanlage des Pflegeheims festgestellt?", None),
    ("Welche Mängel hat die Prüfingenieurin an der Steigleitung beim Hotel Seeblick festgestellt?", None),
    # a date without a year in the text belongs to the year nearest its source (mail of 05.07.2023 / 31.12.2024)
    ("Was wird am 01.09.2024 bei der Tiefgarage eröffnet?", "BS-2022-0901"),
    ("Was ist für den 11.03.2026 bei der Hafenkante vorgeschlagen?", None),
    # the address of a person is not the site address in a report she wrote
    ("Wie lautet die Adresse der Prüfingenieurin?", "BS-2024-0233"),
])
def test_last_wrong_answers_are_refused(demo, question, project):
    s, ai, conn = demo
    r = ask(conn, ai, str(s.db_path), question, project=project)
    assert r.status == "not_found" and not r.sources, r.answer


@pytest.mark.parametrize("question,project,fact", [
    ("Welche Mängel hat die Prüfingenieurin an der Feststellanlage im Pflegeheim festgestellt?", None, "Feststellanlage"),
    ("Was ist für den 11.03.2025 bei der Hafenkante vorgeschlagen?", None, "11. März"),
    ("Wann soll die Tiefgarage eröffnen?", "BS-2022-0901", "1. September"),
    ("Wie lautet die Adresse des Hotels Seeblick?", "BS-2024-0233", "Uferpromenade 15"),
    ("Ist der Mangel an der Feststellanlage inzwischen behoben?", "BS-2023-0412", "Keine Mängel"),
])
def test_the_right_answers_stay(demo, question, project, fact):
    s, ai, conn = demo
    r = ask(conn, ai, str(s.db_path), question, project=project)
    assert r.status == "answered" and fact in r.answer, r.answer
