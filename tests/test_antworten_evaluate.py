"""`python -m projektakte.evaluate` runs the evaluation set against the configured services and
reports retrieval, answers, "nicht gefunden" and the reranker score distribution."""
import json

from projektakte.evaluate import answer_matches, main, summarize


def test_answer_matching_alternatives_and_all():
    q = {"answer_contains": ["8 Stunden"], "answer_regex": [r"\b8\s*h\b"]}
    assert answer_matches(q, "Betriebsdauer 8 h [1]") and answer_matches(q, "mindestens 8 Stunden")
    assert not answer_matches(q, "mindestens 18 Stunden".replace("18 Stunden", "zwei Tage"))
    both = {"answer_contains": ["1,0 m"], "answer_all": [r"1,0\s*m", r"abgelehnt|Ersatz"]}
    assert answer_matches(both, "Als Ersatz … Breite 1,0 m") and not answer_matches(both, "Breite 1,0 m")


def test_cli_with_fakes(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    out = tmp_path / "ergebnis.json"
    code = main(["--fake", "--json", str(out), "--only", "gs-bma-abk", "--only", "ho-stand",
                 "--only", "x-fahrrad-hotel", "--only", "x-sprachalarm"])
    assert code == 0
    text = capsys.readouterr().out
    assert "Fake (im Prozess)" in text and "Zusammenfassung" in text and "Schwellwert" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    recs = {r["id"]: r for r in data["records"]}
    assert set(recs) == {"gs-bma-abk", "ho-stand", "x-fahrrad-hotel", "x-sprachalarm"}
    assert all(r["passed"] for r in recs.values()), {k: r["reasons"] for k, r in recs.items()}
    assert recs["gs-bma-abk"]["rank"] <= 5 and recs["x-fahrrad-hotel"]["status"] == "not_found"
    s = data["summary"]
    assert s["retrieval_top5"] == 1.0 and s["not_found_correct"] == 1.0 and s["false_not_found"] == 0.0
    assert s["rerank_relevant"]["n"] == 3 and len(s["thresholds"]) >= 5
    assert (tmp_path / "data" / "eval" / "projektakte.sqlite3").exists()  # never the production index
    assert not (tmp_path / "data" / "projektakte.sqlite3").exists()


def test_summary_threshold_table():
    from projektakte.evaluate import Record
    def rec(i, nf, rel, other, passed=True, status="answered"):
        return Record(i, "x", "?", None, nf, None if nf else 1, status, passed, [], "", [], rel, other, 1.0, 2.0, 1.0)
    s = summarize([rec("a", False, 0.9, 0.2), rec("b", False, 0.04, 0.01), rec("n", True, None, 0.03,
                                                                               status="not_found")])
    row = {t["threshold"]: t for t in s["thresholds"]}
    assert row[0.05]["relevant_cut"] == 0.5 and row[0.02]["negatives_pass"] == 1.0
    assert row[0.05]["negatives_pass"] == 0.0 and s["answers_passed"] == 1.0


# --------------------------------------------------------------------------- round 2
def _write_set(path, questions):
    path.write_text(json.dumps(questions, ensure_ascii=False), encoding="utf-8")
    return path


QUESTIONS = [
    {"id": "geheim-1", "project": "BS-2024-0233", "question": "Welche Betriebsdauer muss die Sicherheitsbeleuchtung "
     "haben?", "sources": [{"file": "01 Genehmigung/Baugenehmigung.pdf"}], "answer_contains": ["8 Stunden"],
     "category": "fact"},
    {"id": "geheim-2", "project": "BS-2024-0233", "question": "Welche Sprinkleranlage ist vorgesehen?",
     "sources": [], "answer_contains": [], "category": "negative", "not_found": True},
]


def test_summary_only_prints_no_questions_and_exit_code_follows_targets(tmp_path, capsys, monkeypatch):
    """--summary-only shows only totals (the held-back set stays unseen); exit code 1 = targets missed."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    ok = _write_set(tmp_path / "ok.json", QUESTIONS)
    assert main(["--fake", "--eval", str(ok), "--summary-only"]) == 0
    out = capsys.readouterr().out
    assert "Zusammenfassung" in out and "Ziele erreicht" in out and "Dienste-Vorprüfung: Sprachmodell ok" in out
    assert "geheim" not in out and "Sicherheitsbeleuchtung" not in out and "Sprinkler" not in out
    impossible = dict(QUESTIONS[0], id="geheim-3", answer_contains=["17 Kilometer"])
    bad = _write_set(tmp_path / "bad.json", [impossible, QUESTIONS[1]])
    assert main(["--fake", "--eval", str(bad), "--summary-only", "--no-index"]) == 1
    out = capsys.readouterr().out
    assert "Ziele VERFEHLT" in out and "geheim" not in out and "17 Kilometer" not in out


def test_holdout_set_is_loaded_from_tests(tmp_path, capsys, monkeypatch):
    from projektakte import evaluate

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    held = _write_set(tmp_path / "eval_holdout.json", QUESTIONS)
    monkeypatch.setattr(evaluate, "holdout_path", lambda: held)
    assert evaluate.load_eval(name="holdout") == QUESTIONS
    out = tmp_path / "s.json"
    assert main(["--fake", "--set", "holdout", "--summary-only", "--json", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "records" not in data and data["summary"]["correct"] == 1.0 and data["summary"]["refused"] == 1.0
    assert data["settings"]["set"] == "holdout" and data["services_ok"] is True
    assert "geheim" not in capsys.readouterr().out


def test_unreachable_services_give_exit_code_2(tmp_path, capsys, monkeypatch):
    """Preflight: a run against services that are down says so and does not exit with 0."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # closed again below: nothing listens there
    for var, url in (("LLM_URL", f"http://127.0.0.1:{port}/v1"), ("EMBED_URL", f"http://127.0.0.1:{port}"),
                     ("RERANK_URL", f"http://127.0.0.1:{port}/rerank")):
        monkeypatch.setenv(var, url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HEALTH_TIMEOUT", "1")
    sset = _write_set(tmp_path / "q.json", QUESTIONS[1:])
    assert main(["--real", "--eval", str(sset), "--summary-only"]) == 2
    out = capsys.readouterr().out
    assert "NICHT ERREICHBAR" in out and "WARNUNG" in out


def test_summary_counts_answerable_and_refused():
    from projektakte.evaluate import Record

    def rec(nf, status, top=1, fact=True):
        return Record("i", "c", "?", "P", nf, top, status, True, [], "", [], None, None, 1.0, 1.0, 1.0, fact=fact)
    s = summarize([rec(False, "answered"), rec(False, "answered", top=7), rec(False, "not_found", fact=False),
                   rec(True, "not_found"), rec(True, "no_citation"), rec(True, "answered")])
    assert s["correct"] == 1 / 3 and s["refused"] == 2 / 3 and s["answered_wrongly"] == 1
    assert s["targets_met"] is False and s["by_scope"]["mit Projektfilter"] == {"n": 6, "correct": 3}
