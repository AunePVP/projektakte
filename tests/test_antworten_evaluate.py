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
