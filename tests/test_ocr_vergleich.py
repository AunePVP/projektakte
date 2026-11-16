"""The OCR comparison: page images exported from the pages waiting for the model (projektakte.ocr_testseiten), and the
standalone script scripts/ocr_vergleich.py that sends them to OpenAI-compatible servers and writes the comparison."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from projektakte import extract as extract_mod
from projektakte.ocr_testseiten import _candidates, _named, export

from .test_bildauswertung_stufen import Vision, _setup, _stage1, ocr_layer_scan, plain_scan

ROOT = Path(__file__).resolve().parents[1]


def _script():
    spec = importlib.util.spec_from_file_location("ocr_vergleich", ROOT / "scripts" / "ocr_vergleich.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def test_the_comparison_script_against_a_local_server(tmp_path, fake_ai):
    mod = _script()
    pages = tmp_path / "ocr-test"
    pages.mkdir()
    (pages / "001_Plan_S1.png").write_bytes(extract_mod._encode(__import__("PIL.Image").Image.new("L", (64, 64)))[0])
    (pages / "001_Plan_S1.tesseract.txt").write_text("Tesseract-Text", encoding="utf-8")
    spec = f"Fake|{fake_ai.base_url}/v1|fake"
    rows = mod.run(pages, [mod.parse_model(spec)], parallel=2, max_tokens=50, timeout=10, out=tmp_path / "ergebnis")
    assert len(rows) == 1 and not rows[0]["fehler"]
    report = (tmp_path / "ergebnis" / "vergleich.html").read_text(encoding="utf-8")
    assert "001_Plan_S1.png" in report and "Tesseract-Text" in report and "Fake" in report
    assert (tmp_path / "ergebnis" / "zusammenfassung.csv").exists()
    assert mod.summary(rows)[1].startswith("Fake | 1 |")


def test_the_scripts_loop_check_matches_the_apps():
    mod = _script()
    assert mod.looped("Kopf\n" + "| BSK 12 | geprüft | i.O. | Wartung 03/2024 |\n" * 40)
    assert mod.looped("Text " + " ".join(f"[Stempel: {k}]" for k in range(12)))
    assert not mod.looped("\n".join(f"| BSK {i} | geprüft |" for i in range(80)))
    assert mod.parse_model("A|http://x/v1|m|OCR:")["prompt"] == "OCR:"
