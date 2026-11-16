"""Training data for a fine-tuned page reader: the target prompt (scripts/ocr_vorgabe.py), training pages from every
kind of document (projektakte.ocr_testseiten --training), drafts (scripts/ocr_entwurf.py) and the correction tool
(scripts/ocr_korrektur.py) that writes the target answers and exports them as JSONL."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from projektakte import extract as extract_mod
from projektakte.ocr_testseiten import _excluded, _training_candidates, export

from .test_bildauswertung_stufen import Vision, _setup, _stage1, ocr_layer_scan, plain_scan

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(SCRIPTS))


def test_the_target_form_round_trips():
    v = _load("ocr_vorgabe")
    assert v.split("TYP: Plan\nSchriftfeld: Grundriss EG") == ("Plan", "Schriftfeld: Grundriss EG")
    assert v.split("**TYP: Leer**") == ("Leer", "")
    assert v.split("Grundriss EG") == (None, "Grundriss EG")  # no type line
    assert v.join("Leer", "irgendwas") == "TYP: Leer"  # an empty page has nothing after its type
    assert "Raumnamen" in v.PROMPT and "nichts" in v.PROMPT


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


def test_drafts_are_written_in_the_target_form(tmp_path, monkeypatch):
    entwurf = _load("ocr_entwurf")
    for k in range(3):
        Image.new("L", (64, 64), "white").save(tmp_path / f"00{k}_seite.png")
    answers = iter(["TYP: Plan\nGrundriss EG, M 1:100", "Ohne Typzeile", "TYP: Leer"])
    monkeypatch.setattr(entwurf, "ask", lambda *a, **k: {"text": next(answers), "error": None, "finish": "stop"})
    stats = entwurf.draft(tmp_path, {"url": "x", "model": "m"}, parallel=1, max_tokens=50, timeout=5, extra=None)
    assert stats == {"seiten": 3, "fehler": 0, "ohne_typ": 1, "schleife": 0}
    texts = [(tmp_path / f"00{k}_seite.entwurf.txt").read_text(encoding="utf-8") for k in range(3)]
    assert texts == ["TYP: Plan\nGrundriss EG, M 1:100", "TYP: Text\nOhne Typzeile", "TYP: Leer"]
    assert entwurf.draft(tmp_path, {"url": "x", "model": "m"}, parallel=1, max_tokens=50, timeout=5,
                         extra=None)["seiten"] == 0  # done pages are not asked again


def test_the_correction_tool_saves_and_exports(tmp_path):
    k = _load("ocr_korrektur")
    for n in range(3):
        Image.new("L", (64, 64), "white").save(tmp_path / f"00{n}_seite.png")
    (tmp_path / "000_seite.entwurf.txt").write_text("TYP: Plan\nGrundriss", encoding="utf-8")
    tool = k.Tool(tmp_path)
    server = k.ThreadingHTTPServer(("127.0.0.1", 0), k.handler(tool))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        page = opener.open(f"{base}/seite/0").read().decode()
        assert "Grundriss" in page and "value='Plan' accesskey='2' checked" in page and "0 von 3 korrigiert" in page
        assert opener.open(f"{base}/bild/0").read().startswith(b"\x89PNG")
        data = urllib.parse.urlencode({"typ": "Plan", "text": "Grundriss EG\r\nIndex C", "aktion": "speichern"}).encode()
        resp = opener.open(urllib.request.Request(f"{base}/seite/0", data=data, method="POST"))
        assert resp.url.endswith("/seite/1")  # on to the next open page
        data = urllib.parse.urlencode({"typ": "Text", "text": "", "aktion": "verwerfen"}).encode()
        opener.open(urllib.request.Request(f"{base}/seite/1", data=data, method="POST"))
        data = urllib.parse.urlencode({"typ": "Leer", "text": "Logo", "aktion": "speichern"}).encode()
        opener.open(urllib.request.Request(f"{base}/seite/2", data=data, method="POST"))
    finally:
        server.shutdown()
    assert (tmp_path / "000_seite.soll.txt").read_text(encoding="utf-8") == "TYP: Plan\nGrundriss EG\nIndex C"
    assert (tmp_path / "001_seite.nicht-verwenden").exists()
    assert (tmp_path / "002_seite.soll.txt").read_text(encoding="utf-8") == "TYP: Leer"
    counts = k.export(tmp_path, tmp_path / "training.jsonl")
    lines = [json.loads(x) for x in (tmp_path / "training.jsonl").read_text(encoding="utf-8").splitlines()]
    assert counts["Plan"] == 1 and counts["Leer"] == 1 and len(lines) == 2  # the discarded page is left out
    assert lines[0]["messages"][0]["content"].startswith("<image>") and lines[0]["images"][0].endswith("000_seite.png")
    assert lines[0]["messages"][1]["content"] == "TYP: Plan\nGrundriss EG\nIndex C"
