"""Held-back evaluation set (tests/eval_holdout.json), written independently of the fake AI.

Only aggregate numbers are checked and reported – no question text, id or answer appears in a
message, so the set stays unseen by whoever tunes the system (same rule as
``python -m projektakte.evaluate --set holdout --summary-only``).

Targets: answerable questions – expected source among the top 5 hits and the expected fact in the
answer – at least 80 %; questions without an answer – "nicht gefunden" or unbelegt, never presented
as confirmed – at least 90 %. The answer target is not met by the fake reader yet (see the reason of
the xfail marker); it is not lowered: the test reports the real share and passes as soon as the
target is reached. A floor below it guards against regressions.
"""
import sqlite3

import pytest

from projektakte.ai.services import AIServices
from projektakte.demo import generate
from projektakte.evaluate import TARGET_CORRECT, TARGET_REFUSED, evaluate_question, holdout_path, load_eval, summarize
from projektakte.indexer import run_index

from .conftest import fake_settings

REGRESSION_FLOOR = 0.55  # share of answerable questions reached in round 2 (16 of 28) – not the target

pytestmark = pytest.mark.skipif(not holdout_path().exists(), reason="tests/eval_holdout.json fehlt")


@pytest.fixture(scope="module")
def summary(tmp_path_factory, fake_ai):
    tmp = tmp_path_factory.mktemp("holdout")
    generate(tmp / "demo")
    s = fake_settings(fake_ai, tmp, DEMO_MODE=1)
    ai = AIServices.from_settings(s)
    run_index(s, ai)
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    records = [evaluate_question(conn, ai, str(s.db_path), q, s) for q in load_eval(name="holdout")]
    return summarize(records)


def _numbers(s: dict) -> str:
    return (f"beantwortbar richtig {s['correct']:.2f} von {s['answerable']}, ohne Antwort richtig abgelehnt "
            f"{s['refused']:.2f} von {s['unanswerable']}, nach Art {s['by_category']}, Fehlerarten {s['failures']}")


def test_unanswerable_questions_are_never_answered_as_confirmed(summary):
    assert summary["unanswerable"] and summary["refused"] >= TARGET_REFUSED, _numbers(summary)


@pytest.mark.xfail(strict=False, reason="Ziel ≥ 80 % für beantwortbare Fragen mit der Fake-KI noch nicht erreicht "
                                        "(Stand Runde 2: 16 von 28 = 0,57) – die echte Zahl steht in der Meldung")
def test_answerable_questions_reach_the_target(summary):
    assert summary["answerable"] and summary["correct"] >= TARGET_CORRECT, _numbers(summary)


def test_answerable_questions_do_not_regress(summary):
    assert summary["correct"] >= REGRESSION_FLOOR, _numbers(summary)
