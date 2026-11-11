"""Another vision model (e.g. a small transcription model instead of the large one) is no reason to read
stage 1 again or to put finished projects back into the queue: the phase "Bildauswertung" goes on with it."""
from __future__ import annotations

from projektakte import reihenfolge
from projektakte.documents import reprocess_due
from projektakte.extract import profile_key


def test_the_model_name_is_not_part_of_the_key():
    assert profile_key("v7:vision:qwen3.8-flash-next+ocr") == profile_key("v7:vision:qwen35-4b+ocr")
    assert profile_key("v7:vision:a:b+ocr:ohne-fotos") == "v7:vision+ocr:ohne-fotos"
    assert profile_key("v7:vision:groß") != profile_key("v7:text")  # vision switched on still counts
    assert profile_key("v7:vision:m") != profile_key("v7:vision:m+ocr")  # Tesseract installed still counts
    assert profile_key("v6:vision:m") != profile_key("v7:vision:m")


class _Row(dict):
    def keys(self):
        return list(super().keys())


def _row(profile, incomplete=1, retry_after=None, pending=0):
    return _Row(incomplete=incomplete, extract_profile=profile, retry_after=retry_after, vision_pending=pending)


def test_an_incomplete_document_is_not_read_again_for_another_model():
    now = "2026-11-12 10:00:00"
    # scan pages waiting for stage 2: the phase goes on with the new model, stage 1 is not repeated
    assert not reprocess_due(_row("v7:vision:gross+ocr", pending=3), "v7:vision:klein+ocr", now)
    # the old model answered emptily (an image read in one go): the new one tries again
    assert reprocess_due(_row("v7:vision:gross+ocr"), "v7:vision:klein+ocr", now)
    assert reprocess_due(_row("v7:text+ocr", pending=3), "v7:vision:klein+ocr", now)


def test_a_finished_project_stays_finished(tmp_path):
    from projektakte import db
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    conn.execute("INSERT INTO projects (number, name, path, active, done_at, done_profile) "
                 "VALUES ('310100', 'Neubau Kita', ?, 1, '2026-11-11 08:00:00', 'v7:vision:gross+ocr')",
                 (str(tmp_path / "3" / "310100_Neubau Kita"),))
    done = {e["number"]: e["done"] for e in reihenfolge.entries(conn, tmp_path, "v7:vision:klein+ocr")}
    assert done["310100"] is True
    done = {e["number"]: e["done"] for e in reihenfolge.entries(conn, tmp_path, "v7:text+ocr")}
    assert done["310100"] is False
