"""Pages for the OCR model comparison (projektakte.ocr_testseiten); the comparison tools themselves live in the
repository aunepvp/tune."""
from __future__ import annotations

import json
from pathlib import Path

from projektakte import extract as extract_mod
from projektakte.ocr_testseiten import _candidates, _named, export

from .test_bildauswertung_stufen import Vision, _setup, _stage1, ocr_layer_scan, plain_scan

def test_export_writes_page_images_with_their_text(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mod, "ocr_available", lambda: True)
    monkeypatch.setattr(extract_mod, "ocr_image", lambda data: "Seite gescannt Rauchabzug Treppenraum erkannt")
    conn, root = _setup(tmp_path, {"Protokoll.pdf": ocr_layer_scan(), "Scan.pdf": plain_scan(3)})
    _stage1(conn, root, Vision())
    wanted = _candidates(conn, 10, seed=1)
    assert len(wanted) == 3  # at most two pages per document
    settings = type("S", (), {"projects_dir": root})()
    out = export(conn, settings, tmp_path / "ocr-test", wanted)
    assert len(out) == 3
    listed = json.loads((tmp_path / "ocr-test" / "seiten.json").read_text(encoding="utf-8"))
    assert [e["datei"] for e in listed] == [e["datei"] for e in out]
    for e in out:
        assert (tmp_path / "ocr-test" / e["datei"]).stat().st_size > 0
        stem = Path(e["datei"]).stem
        assert (tmp_path / "ocr-test" / f"{stem}.tesseract.txt").exists()
    assert _named(conn, "Scan", [2]) == [(conn.execute("SELECT id FROM documents WHERE filename = 'Scan.pdf'")
                                          .fetchone()[0], 2)]
    # nothing changed in the database
    assert conn.execute("SELECT COUNT(*) FROM page_texts WHERE state = 'pending'").fetchone()[0] == 4
    # a second export into the same folder replaces the first: no old pages mixed in (seen: 100 pages, 50 listed)
    (tmp_path / "ocr-test" / "099_alt_S1.jpg").write_bytes(b"x")
    (tmp_path / "ocr-test" / "notiz.txt").write_text("bleibt", encoding="utf-8")
    again = export(conn, settings, tmp_path / "ocr-test", wanted[:1])
    images = sorted(p.name for p in (tmp_path / "ocr-test").iterdir() if p.suffix in (".jpg", ".png"))
    assert images == [again[0]["datei"]] and (tmp_path / "ocr-test" / "notiz.txt").exists()


def test_pages_from_cad_and_bilder_folders_are_left_out(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mod, "ocr_available", lambda: True)
    monkeypatch.setattr(extract_mod, "ocr_image", lambda data: "Seite gescannt Rauchabzug Treppenraum erkannt")
    conn, root = _setup(tmp_path, {"Text/Protokoll.pdf": ocr_layer_scan(), "BS-2024-0001-CAD/Plan.pdf": plain_scan(),
                                   "Bilder/Foto.pdf": plain_scan()})
    _stage1(conn, root, Vision())
    names = {conn.execute("SELECT filename FROM documents WHERE id = ?", (d,)).fetchone()[0]
             for d, _p in _candidates(conn, 10, seed=1)}
    assert names == {"Protokoll.pdf"}
    assert len(_candidates(conn, 10, seed=1, skip=())) == 3  # --ohne-ordner "": all
