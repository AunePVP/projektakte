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


def test_extra_body_goes_into_every_request(tmp_path, monkeypatch):
    """Qwen thought before answering in the script (2.628 tokens for 646 characters) – the app switches that off with
    LLM_EXTRA_BODY; --extra-body sends the same."""
    mod = _script()
    sent = []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body

    class Opener:
        def open(self, req, timeout):
            sent.append(json.loads(req.data))
            return Resp(json.dumps({"choices": [{"message": {"content": "Text", "reasoning_content": "hm"},
                                                 "finish_reason": "stop"}],
                                    "usage": {"completion_tokens": 3}}).encode())

    monkeypatch.setattr(mod.urllib.request, "build_opener", lambda *a: Opener())
    res = mod.ask("http://x/v1", "m", "OCR:", b"img", "image/png", 50, 5,
                  {"chat_template_kwargs": {"enable_thinking": False}})
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False} and res["thinking"] and res["text"] == "Text"
