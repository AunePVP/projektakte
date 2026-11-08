"""ASK_DEADLINE: a question never waits longer than its budget, whatever the AI services do."""
import sqlite3
import time

from projektakte.ai.services import AIServices
from projektakte.ask import ask
from projektakte.demo import generate
from projektakte.indexer import run_index

from .conftest import fake_settings


def test_slow_services_are_cut_off_by_the_question_budget(tmp_path, fresh_fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fresh_fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    fresh_fake_ai.options["delay"] = 3.0  # every AI call hangs for 3 s
    started = time.monotonic()
    r = ask(conn, ai, str(s.db_path), "Wie hoch ist der Löschwasserbedarf der Industriehalle?",
            project="BS-2023-0777", deadline_s=2.0)
    elapsed = time.monotonic() - started
    assert elapsed < 2.6, elapsed  # without the budget: 3 s embedding + 3 s reranker + 3 s LLM
    assert r.status == "llm_unavailable" and r.context  # the full-text passages come back
    fresh_fake_ai.options["delay"] = 0


def test_without_a_budget_nothing_changes(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path)
    conn.row_factory = sqlite3.Row
    for budget in (0, 180):
        r = ask(conn, ai, str(s.db_path), "Wie hoch ist der Löschwasserbedarf der Industriehalle?",
                project="BS-2023-0777", deadline_s=budget)
        assert r.status == "answered" and "192" in r.answer
