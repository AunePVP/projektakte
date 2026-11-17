"""Training pages for a fine-tuned page reader (projektakte.ocr_testseiten --training): every kind of document, without
the comparison pages. Drafts, correction and training live in the repository aunepvp/tune."""
from __future__ import annotations

import json

from PIL import Image

from projektakte import extract as extract_mod
from projektakte.ocr_testseiten import _excluded, _training_candidates, export

from .test_bildauswertung_stufen import Vision, _setup, _stage1, ocr_layer_scan, plain_scan

def test_training_pages_mix_scans_cad_digital_and_photos(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mod, "ocr_available", lambda: False)
    png = tmp_path / "x.png"
    Image.new("RGB", (80, 60), "white").save(png)
    conn, root = _setup(tmp_path, {"Text/Scan.pdf": plain_scan(2), "Text/Bericht.pdf": ocr_layer_scan(),
                                   "BS-2024-0001-CAD/Plan.pdf": plain_scan(), "Bilder/Foto.png": png.read_bytes()})
    _stage1(conn, root, Vision())
    conn.execute("UPDATE documents SET vision_pending = 0, pages = 1 WHERE filename = 'Bericht.pdf'")
    conn.execute("DELETE FROM page_texts WHERE doc_id = (SELECT id FROM documents WHERE filename = 'Bericht.pdf')")
    conn.commit()
    picked = _training_candidates(conn, 20, seed=1)
    kinds = {k for _d, _p, k in picked}
    assert {"scan", "cad", "digital", "foto"} <= kinds
    settings = type("S", (), {"projects_dir": root})()
    out = export(conn, settings, tmp_path / "training", picked)
    assert {e["quelle"] for e in out} == kinds and any(e["datei"].endswith(".png") for e in out)
    # the comparison pages stay out of the training
    (tmp_path / "pruef.json").write_text(json.dumps([{"dokument": e["dokument"], "seite": e["seite"]} for e in out
                                                     if e["quelle"] == "scan"]), encoding="utf-8")
    again = _training_candidates(conn, 20, seed=1, exclude=_excluded([str(tmp_path / "pruef.json")]))
    assert "scan" not in {k for _d, _p, k in again}
