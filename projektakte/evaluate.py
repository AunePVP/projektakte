"""Measure answer quality: ``python -m projektakte.evaluate [--real|--fake] [--set eval|holdout] [--json DATEI]``.

Runs an evaluation set (questions with known answers in the demo data) against the configured AI
services and prints per question whether the expected source is among the top 5 hits, whether the
answer contains the expected fact and cites an expected source, whether "nicht gefunden" was right,
the reranker scores and the latency. The summary shows the score distribution of relevant and
irrelevant hits and what a threshold would cut off – the basis for calibrating ``ASK_MIN_RERANK`` and
``SEARCH_MIN_RERANK`` with the real models.

Sets:

* ``eval`` (default) – ``projektakte/demo/eval.py`` (= tests/eval.json), written together with the
  demo data and the fake AI;
* ``holdout`` – ``tests/eval_holdout.json``, questions written independently by people who did not
  build the fake services. ``--summary-only`` prints only aggregate numbers per category (no question
  texts, ids or answers), so the set stays unseen by whoever tunes the system.

Before the run the services are probed (as on /health/deps). Exit code: 0 = targets met, 1 = targets
missed (answerable questions: expected source in the top 5 and the fact in the answer, default
>= 80 %; questions without an answer: "nicht gefunden" or unbelegt, default >= 90 %), 2 = a service was
not reachable (the numbers then say little), the set could not be read or no question was selected
(``--only``/``--category`` matched nothing, empty set). A target without questions of its kind is
reported as "nicht geprüft".

The demo data is indexed into a separate data folder (default ``$DATA_DIR/eval``), never into the
production index. The same checks are used by the tests (tests/test_ask.py, tests/test_search.py,
tests/test_antworten_holdout.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ask import AskResult, ask
from .search import Hit, hybrid_search, load_hits

THRESHOLDS = (0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5)
TARGET_CORRECT = 0.8  # answerable questions: expected source in the top 5 and the fact in the answer
TARGET_REFUSED = 0.9  # questions without an answer: not_found or no_citation, never "answered"
REFUSED_STATUSES = ("not_found", "no_citation")
SETS = ("eval", "holdout")


# --------------------------------------------------------------------------- checks (shared with tests)
def source_matches(conn: sqlite3.Connection, hit: Hit, src: dict, q: dict) -> bool:
    """Is ``hit`` one of the acceptable sources ``src`` of question ``q``?"""
    project = src.get("project") or q.get("project")
    if project and hit.project != project:
        return False
    if "file" in src:
        return hit.kind == "document" and hit.rel_path == src["file"]
    if "attachment" in src:
        return hit.kind == "attachment" and hit.title == src["attachment"]
    if "mail_subject" in src:
        if not hit.mail_id:
            return False
        row = conn.execute("SELECT subject FROM mails WHERE id = ?", (hit.mail_id,)).fetchone()
        return bool(row) and row[0] == src["mail_subject"]
    return bool(project)


def answer_matches(q: dict, answer: str) -> bool:
    """Expected fact in the answer: any of ``answer_contains``/``answer_regex``, all of ``answer_all``."""
    if any(not re.search(p, answer, re.I) for p in q.get("answer_all", [])):
        return False
    alternatives = [a[3:] if a.startswith("re:") else re.escape(a) for a in q.get("answer_contains", [])]
    alternatives += list(q.get("answer_regex", []))
    return not alternatives or any(re.search(p, answer, re.I) for p in alternatives)


@dataclass
class Check:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def check_answer(conn: sqlite3.Connection, q: dict, r: AskResult) -> Check:
    """Does the answer to ``q`` meet the evaluation criteria? (reasons in German for reports)"""
    reasons: list[str] = []
    if q.get("not_found"):
        if r.status != "not_found":
            reasons.append(f"„nicht gefunden“ erwartet, Status {r.status}")
        return Check(not reasons, reasons)
    if r.status != "answered":
        reasons.append(f"Status {r.status}" + (" (falsches „nicht gefunden“)" if r.status == "not_found" else ""))
        return Check(False, reasons)
    if not answer_matches(q, r.answer):
        reasons.append("erwarteter Fakt fehlt in der Antwort")
    hits = load_hits(conn, [s["chunk_id"] for s in r.sources])
    cited = [hits[s["chunk_id"]] for s in r.sources if s["chunk_id"] in hits]
    if not any(source_matches(conn, h, src, q) for h in cited for src in q["sources"]):
        reasons.append("keine erwartete Quelle zitiert")
    if q.get("expected_projects"):
        expected = set(q["expected_projects"])
        projects = {h.project for h in cited}
        wrong = projects - expected
        if wrong:
            reasons.append("falsche Projekte zitiert: " + ", ".join(sorted(p or "–" for p in wrong)))
        if len(projects & expected) < q.get("min_projects", 1):
            reasons.append(f"nur {len(projects & expected)} von mindestens {q.get('min_projects', 1)} Projekten")
    return Check(not reasons, reasons)


def expected_rank(conn: sqlite3.Connection, q: dict, hits: list[Hit]) -> int | None:
    """1-based rank of the first expected source among ``hits`` (None = not found)."""
    for i, h in enumerate(hits, start=1):
        if any(source_matches(conn, h, src, q) for src in q["sources"]):
            return i
    return None


def holdout_path() -> Path:
    """tests/eval_holdout.json of the source tree (the independent, held-back question set)."""
    return Path(__file__).resolve().parent.parent / "tests" / "eval_holdout.json"


def load_eval(path: Path | None = None, *, name: str = "eval") -> list[dict]:
    """Questions of an evaluation set: an explicit JSON file, ``eval`` (built-in) or ``holdout``."""
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    if name == "holdout":
        return json.loads(holdout_path().read_text(encoding="utf-8"))
    if name != "eval":
        raise ValueError(f"unbekannter Evaluationssatz: {name}")
    from .demo.eval import EVAL

    return EVAL


# --------------------------------------------------------------------------- running
@dataclass
class Record:
    id: str
    category: str
    question: str
    project: str | None
    not_found_expected: bool
    rank: int | None  # of the expected source in the search (top 30), None = missing
    status: str
    passed: bool
    reasons: list[str]
    answer: str
    cited: list[str]
    relevant_rerank: float | None  # best reranker score of an expected source
    other_rerank: float | None  # best reranker score of any other hit
    search_ms: float
    ask_ms: float
    llm_ms: float | None
    fact: bool | None = None  # expected fact in an "answered" answer (None for questions without answer)
    fact_in_context: bool | None = None  # expected fact in one of the passages given to the model

    @property
    def top5(self) -> bool | None:
        return None if self.not_found_expected else (self.rank is not None and self.rank <= 5)

    @property
    def correct(self) -> bool:
        """Target criterion: answerable → expected source in the top 5 and the fact in the answer;
        without answer → "nicht gefunden" or unbelegt (no answer presented as confirmed)."""
        if self.not_found_expected:
            return self.status in REFUSED_STATUSES
        return bool(self.top5) and self.status == "answered" and bool(self.fact)


def evaluate_question(conn: sqlite3.Connection, ai, vector_key: str, q: dict, settings) -> Record:
    t0 = time.perf_counter()
    res = hybrid_search(conn, ai, vector_key, q["question"], project=q.get("project"), k=settings.rerank_top_k,
                        rerank_top_k=settings.rerank_top_k)
    search_ms = (time.perf_counter() - t0) * 1000
    rel = [h.rerank for h in res.hits if h.rerank is not None and q["sources"]
           and any(source_matches(conn, h, s, q) for s in q["sources"])]
    other = [h.rerank for h in res.hits if h.rerank is not None and not (
        q["sources"] and any(source_matches(conn, h, s, q) for s in q["sources"]))]
    t0 = time.perf_counter()
    r = ask(conn, ai, vector_key, q["question"], project=q.get("project"), top_k=settings.ask_top_k,
            context_chars=settings.ask_context_chars, min_rerank=settings.ask_min_rerank,
            rerank_top_k=settings.rerank_top_k)
    ask_ms = (time.perf_counter() - t0) * 1000
    chk = check_answer(conn, q, r)
    fact = None if q.get("not_found") else (r.status == "answered" and answer_matches(q, r.answer))
    in_context = None if q.get("not_found") else any(
        (q.get("answer_contains") or q.get("answer_regex")) and answer_matches(q, c.get("text") or "") for c in r.context)
    return Record(
        id=q["id"], category=q.get("category", ""), question=q["question"], project=q.get("project"),
        not_found_expected=bool(q.get("not_found")), rank=expected_rank(conn, q, res.hits) if q["sources"] else None,
        status=r.status, passed=chk.passed, reasons=chk.reasons, answer=r.answer,
        cited=[f"[{s['n']}] {s['label']}" for s in r.sources],
        relevant_rerank=max(rel) if rel else None, other_rerank=max(other) if other else None,
        search_ms=search_ms, ask_ms=ask_ms, llm_ms=r.latency_ms, fact=fact, fact_in_context=in_context)


def _pct(values: list[float], p: float) -> float:
    values = sorted(values)
    if not values:
        return float("nan")
    i = min(len(values) - 1, max(0, round(p * (len(values) - 1))))
    return values[i]


def _failure_kind(reason: str) -> str:
    """Reason of a failed check without question-specific details (for --summary-only)."""
    if reason.startswith("„nicht gefunden“ erwartet"):
        return "beantwortet statt „nicht gefunden“"
    if reason.startswith("Status not_found"):
        return "falsches „nicht gefunden“"
    if reason.startswith("Status "):
        return reason.split(" (")[0]
    if reason.startswith("falsche Projekte"):
        return "falsche Projekte zitiert"
    if reason.startswith("nur ") and "Projekten" in reason:
        return "zu wenige Projekte"
    return reason


def summarize(records: list[Record], *, target_correct: float = TARGET_CORRECT,
              target_refused: float = TARGET_REFUSED) -> dict:
    pos = [r for r in records if not r.not_found_expected]
    neg = [r for r in records if r.not_found_expected]
    rel = [r.relevant_rerank for r in pos if r.relevant_rerank is not None]
    neg_top = [max(x for x in (r.relevant_rerank, r.other_rerank) if x is not None) for r in neg
               if r.other_rerank is not None or r.relevant_rerank is not None]
    other = [r.other_rerank for r in pos if r.other_rerank is not None]

    def dist(values: list[float]) -> dict:
        return {"n": len(values), "min": min(values, default=None), "p10": _pct(values, 0.1) if values else None,
                "median": statistics.median(values) if values else None,
                "p90": _pct(values, 0.9) if values else None, "max": max(values, default=None)}

    thresholds = []
    for t in THRESHOLDS:
        thresholds.append({
            "threshold": t,
            "relevant_cut": sum(1 for v in rel if v < t) / len(rel) if rel else None,
            "negatives_pass": sum(1 for v in neg_top if v >= t) / len(neg_top) if neg_top else None,
        })
    correct = sum(1 for r in pos if r.correct) / len(pos) if pos else None
    refused = sum(1 for r in neg if r.correct) / len(neg) if neg else None
    failures: dict[str, int] = {}
    for r in records:
        for kind in dict.fromkeys(_failure_kind(x) for x in r.reasons):
            failures[kind] = failures.get(kind, 0) + 1
    categories = {}
    for c in sorted({r.category for r in records}):
        rs = [r for r in records if r.category == c]
        kinds: dict[str, int] = {}
        for r in rs:
            for kind in dict.fromkeys(_failure_kind(x) for x in r.reasons):
                kinds[kind] = kinds.get(kind, 0) + 1
        categories[c] = {"n": len(rs), "passed": sum(1 for r in rs if r.passed),
                         "correct": sum(1 for r in rs if r.correct),
                         "answerable": sum(1 for r in rs if not r.not_found_expected), "failures": kinds}
    scopes = {}
    for label, rs in (("mit Projektfilter", [r for r in records if r.project]),
                      ("ohne Projektfilter", [r for r in records if not r.project])):
        if rs:
            scopes[label] = {"n": len(rs), "correct": sum(1 for r in rs if r.correct)}
    return {
        "questions": len(records),
        "answerable": len(pos),
        "unanswerable": len(neg),
        "correct": correct,
        "refused": refused,
        "targets": {"correct": target_correct, "refused": target_refused},
        "targets_met": (correct is None or correct >= target_correct - 1e-9)
        and (refused is None or refused >= target_refused - 1e-9),
        "answered_wrongly": sum(1 for r in neg if r.status == "answered"),
        # reader or retrieval? answerable questions missed although the fact was in the context
        "missed_with_fact_in_context": sum(1 for r in pos if not r.correct and r.fact_in_context),
        "missed_without_fact_in_context": sum(1 for r in pos if not r.correct and r.fact_in_context is False),
        "failures": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
        "by_category_detail": categories,
        "by_scope": scopes,
        "retrieval_top5": sum(1 for r in pos if r.top5) / len(pos) if pos else None,
        "answers_passed": sum(1 for r in pos if r.passed) / len(pos) if pos else None,
        "false_not_found": sum(1 for r in pos if r.status == "not_found") / len(pos) if pos else None,
        "not_found_correct": sum(1 for r in neg if r.passed) / len(neg) if neg else None,
        "passed": sum(1 for r in records if r.passed),
        "rerank_relevant": dist(rel),
        "rerank_other_in_positive": dist(other),
        "rerank_top_in_negative": dist(neg_top),
        "thresholds": thresholds,
        "ask_ms_median": statistics.median([r.ask_ms for r in records]) if records else None,
        "ask_ms_max": max((r.ask_ms for r in records), default=None),
        "by_category": {c: f"{sum(1 for r in records if r.category == c and r.passed)}/"
                           f"{sum(1 for r in records if r.category == c)}"
                        for c in sorted({r.category for r in records})},
    }


def _fmt(v, digits: int = 3) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "–"
    return f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def print_report(records: list[Record], summary: dict, out=None, *, services: dict | None = None) -> None:
    w = (out or sys.stdout).write
    w(f"{'Frage':26} {'Art':12} {'Top5':5} {'Status':15} {'ok':3} {'Rerank rel/and':15} {'ms':>6}\n")
    w("-" * 88 + "\n")
    for r in records:
        top5 = "–" if r.top5 is None else ("ja" if r.top5 else f"nein({r.rank or '-'})")
        rr = f"{_fmt(r.relevant_rerank, 2)}/{_fmt(r.other_rerank, 2)}"
        w(f"{r.id[:26]:26} {r.category[:12]:12} {top5:5} {r.status[:15]:15} {'ja' if r.passed else 'NEIN':3} "
          f"{rr:15} {r.ask_ms:6.0f}\n")
        if not r.passed:
            w(f"    → {'; '.join(r.reasons)} | {r.answer[:160]}\n")
    print_summary(summary, out, services=services)


def _services_line(services: dict) -> str:
    names = {"llm": "Sprachmodell", "embeddings": "Embeddings", "reranker": "Reranker"}
    parts = []
    for key, label in names.items():
        info = services.get(key) or {}
        parts.append(f"{label} {'ok' if info.get('ok') else 'NICHT ERREICHBAR'}")
    return ", ".join(parts)


def services_ok(services: dict | None) -> bool:
    return bool(services) and all((services.get(k) or {}).get("ok") for k in ("llm", "embeddings", "reranker"))


def print_summary(summary: dict, out=None, *, services: dict | None = None) -> None:
    """Aggregate numbers only – no question texts, ids or answers (used alone by --summary-only)."""
    w = (out or sys.stdout).write
    s = summary
    w("\nZusammenfassung\n")
    if services is not None:
        w(f"  Dienste-Vorprüfung: {_services_line(services)}\n")
        if not services_ok(services):
            w("  WARNUNG: Nicht alle KI-Dienste waren erreichbar – die Zahlen sagen wenig über die Qualität aus.\n")
    t = s["targets"]
    w(f"  Fragen: {s['questions']} ({s['answerable']} beantwortbar, {s['unanswerable']} ohne Antwort), "
      f"bestanden: {s['passed']}\n")
    def share(value, target: float) -> str:
        return "nicht geprüft (keine solchen Fragen im Satz)" if value is None else f"{value:.2f} (Ziel {target:.2f})"

    w(f"  Beantwortbar richtig (Quelle unter den ersten 5 und Fakt in der Antwort): {share(s['correct'], t['correct'])}\n")
    w(f"  Ohne Antwort richtig abgelehnt („nicht gefunden“ oder unbelegt): {share(s['refused'], t['refused'])}; "
      f"fälschlich als belegt beantwortet: {s['answered_wrongly']}\n")
    w(f"  Ziele {'erreicht' if s['targets_met'] else 'VERFEHLT'}\n")
    w(f"  Erwartete Quelle unter den ersten 5: {_fmt(s['retrieval_top5'], 2)}\n")
    w(f"  Antwort mit Fakt und Quelle: {_fmt(s['answers_passed'], 2)}   falsches „nicht gefunden“: "
      f"{_fmt(s['false_not_found'], 2)}\n")
    w(f"  „Nicht gefunden“ richtig (Fragen ohne Antwort): {_fmt(s['not_found_correct'], 2)}\n")
    w(f"  Nach Art (bestanden): {', '.join(f'{k} {v}' for k, v in s['by_category'].items())}\n")
    w("  Nach Art (Zielkriterium): " + ", ".join(f"{k} {d['correct']}/{d['n']}"
                                                 for k, d in s['by_category_detail'].items()) + "\n")
    if s["failures"]:
        w("  Fehlerarten: " + ", ".join(f"{k}: {v}" for k, v in s["failures"].items()) + "\n")
        for c, d in s["by_category_detail"].items():
            if d.get("failures"):
                w(f"    {c}: " + ", ".join(f"{k}: {v}" for k, v in d["failures"].items()) + "\n")
    if s.get("missed_with_fact_in_context") or s.get("missed_without_fact_in_context"):
        w(f"  Verfehlt, obwohl der Fakt im Kontext stand: {s['missed_with_fact_in_context']}; "
          f"Fakt nicht im Kontext: {s['missed_without_fact_in_context']}\n")
    if s.get("by_scope"):
        w("  Nach Suchbereich (Zielkriterium): " + ", ".join(f"{k} {d['correct']}/{d['n']}"
                                                         for k, d in s["by_scope"].items()) + "\n")
    w(f"  Laufzeit je Frage: Median {_fmt(s['ask_ms_median'], 0)} ms, max {_fmt(s['ask_ms_max'], 0)} ms\n")
    w("\nReranker-Scores (min / p10 / Median / p90 / max)\n")
    for key, label in (("rerank_relevant", "erwartete Quelle"), ("rerank_other_in_positive", "andere Treffer"),
                       ("rerank_top_in_negative", "bester Treffer bei Fragen ohne Antwort")):
        d = s[key]
        w(f"  {label:40} n={d['n']:3}  " + " / ".join(_fmt(d[x]) for x in ("min", "p10", "median", "p90", "max"))
          + "\n")
    w("\nSchwellwert (ASK_MIN_RERANK / SEARCH_MIN_RERANK): Anteil erwarteter Quellen darunter | Anteil Fragen "
      "ohne Antwort, deren bester Treffer ihn erreicht\n")
    for t in s["thresholds"]:
        w(f"  {t['threshold']:>6}: {_fmt(t['relevant_cut'], 2):>5} | {_fmt(t['negatives_pass'], 2):>5}\n")


def build_index(settings, ai) -> None:
    """Demo data into the evaluation folder, indexed with the configured services."""
    from .demo import ensure
    from .indexer import run_index

    ensure(settings.demo_dir)
    run_index(settings, ai)


def eval_settings(data_dir: Path, mode: str | None):
    """Settings for the evaluation run: demo data in ``data_dir``, assignment by project number."""
    from .config import load_settings

    env = dict(os.environ)
    env.update({"DATA_DIR": str(data_dir), "DEMO_MODE": "1", "PROJECTS_DIR": "", "MAIL_DIR": "",
                "PLMAIL_URL": "", "PLMAIL_TOKEN": "", "ASSIGN_MODE": "auto"})
    if mode:
        env["AI_MODE"] = mode
    return load_settings(env)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m projektakte.evaluate",
                                 description="Evaluationssatz gegen die konfigurierten KI-Dienste laufen lassen")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--real", action="store_true", help="echte Dienste (LLM_URL, EMBED_URL, RERANK_URL)")
    mode.add_argument("--fake", action="store_true", help="Fake-KI-Dienste im Prozess")
    ap.add_argument("--json", type=Path, metavar="DATEI", help="Ergebnisse zusätzlich als JSON schreiben")
    ap.add_argument("--set", dest="set_name", choices=SETS, default="eval",
                    help="Evaluationssatz: eval (Standard, tests/eval.json) oder holdout (tests/eval_holdout.json)")
    ap.add_argument("--eval", type=Path, metavar="DATEI", help="anderer Evaluationssatz (JSON wie tests/eval.json)")
    ap.add_argument("--summary-only", action="store_true",
                    help="nur Gesamtzahlen je Art ausgeben (keine Fragen, Antworten oder IDs)")
    ap.add_argument("--min-correct", type=float, default=TARGET_CORRECT, metavar="ANTEIL",
                    help=f"Ziel für beantwortbare Fragen (Standard {TARGET_CORRECT})")
    ap.add_argument("--min-refused", type=float, default=TARGET_REFUSED, metavar="ANTEIL",
                    help=f"Ziel für Fragen ohne Antwort (Standard {TARGET_REFUSED})")
    ap.add_argument("--data-dir", type=Path, help="Ordner für Demo-Daten und Index (Standard: $DATA_DIR/eval)")
    ap.add_argument("--only", action="append", metavar="ID", help="nur diese Frage(n)")
    ap.add_argument("--category", action="append", help="nur Fragen dieser Art (z. B. stand, negative)")
    ap.add_argument("--no-index", action="store_true", help="vorhandenen Index ohne Abgleich verwenden")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from .ai.services import AIServices, resolve_settings
    from .config import load_settings

    base = load_settings()
    data_dir = a.data_dir or (base.data_dir / "eval")
    s = resolve_settings(eval_settings(data_dir, "real" if a.real else "fake" if a.fake else None))
    ai = AIServices.from_settings(s)
    print(f"KI-Dienste: {'Fake (im Prozess)' if s.ai_mode == 'fake' else 'echt'} – LLM {s.llm_url} ({s.llm_model}), "
          f"Embeddings {s.embed_url}, Reranker {s.rerank_url}")
    print(f"ASK_MIN_RERANK={s.ask_min_rerank}  ASK_TOP_K={s.ask_top_k}  RERANK_TOP_K={s.rerank_top_k}  "
          f"Daten: {data_dir}")
    try:
        questions = load_eval(a.eval, name=a.set_name)
    except (OSError, ValueError) as exc:
        print(f"Evaluationssatz nicht lesbar: {exc}", file=sys.stderr)
        return 2
    if a.only:
        questions = [q for q in questions if q["id"] in set(a.only)]
    if a.category:
        questions = [q for q in questions if q.get("category") in set(a.category)]
    if not questions:  # a typo in --only/--category or an empty set must not pass as "targets met"
        print("Keine Fragen ausgewählt – nichts zu prüfen (Exit 2). --only, --category und den "
              "Evaluationssatz prüfen.", file=sys.stderr)
        return 2
    services = ai.check().get("services") or {}
    print(f"Dienste-Vorprüfung: {_services_line(services)}")
    for key in ("llm", "embeddings", "reranker"):
        info = services.get(key) or {}
        if not info.get("ok"):
            print(f"  {key}: {info.get('error') or info.get('hint') or 'nicht erreichbar'}")
    if not a.no_index:
        t0 = time.perf_counter()
        build_index(s, ai)
        print(f"Index aktuell ({time.perf_counter() - t0:.1f} s)")
    conn = sqlite3.connect(s.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    records = [evaluate_question(conn, ai, str(s.db_path), q, s) for q in questions]
    summary = summarize(records, target_correct=a.min_correct, target_refused=a.min_refused)
    if a.summary_only:
        print_summary(summary, services=services)
    else:
        print()
        print_report(records, summary, services=services)
    if a.json:
        data = {"settings": {"ai_mode": s.ai_mode, "llm_model": s.llm_model, "ask_min_rerank": s.ask_min_rerank,
                             "set": a.set_name},
                "services_ok": services_ok(services), "summary": summary}
        if not a.summary_only:
            data["records"] = [r.__dict__ for r in records]
        a.json.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"\nJSON geschrieben: {a.json}")
    if not services_ok(services):
        return 2
    return 0 if summary["targets_met"] else 1


if __name__ == "__main__":
    sys.exit(main())
