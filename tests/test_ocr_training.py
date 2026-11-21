"""Training pages for a fine-tuned page reader (projektakte.ocr_testseiten --training): every kind of document, without
the comparison pages. Drafts, correction and training live in the repository aunepvp/tune."""
from __future__ import annotations

import json

from PIL import Image

from projektakte import extract as extract_mod
from projektakte.ocr_testseiten import _all_candidates, _excluded, _training_candidates, export

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


def test_all_pages_of_every_folder_for_a_type_classifier(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_mod, "ocr_available", lambda: False)
    png = tmp_path / "x.png"
    Image.new("RGB", (80, 60), "white").save(png)
    conn, root = _setup(tmp_path, {"1 Schriftverkehr/Brief.pdf": ocr_layer_scan(), "2 Scans/Scan.pdf": plain_scan(4),
                                   "CAD/Plan.pdf": plain_scan(), "Bilder/Foto.png": png.read_bytes()})
    _stage1(conn, root, Vision())
    picked = _all_candidates(conn, 50, seed=1, per_doc=2)
    docs = {d for d, _p, _k in picked}
    assert len(docs) == 4  # every folder, read pages and waiting ones alike
    assert max(sum(1 for d, _p, _k in picked if d == doc) for doc in docs) == 2  # per document
    assert {k for _d, _p, k in picked} == {"pdf", "cad", "foto"}
    assert len(_all_candidates(conn, 3, seed=1)) == 3
    out = export(conn, type("S", (), {"projects_dir": root})(), tmp_path / "typen", picked)
    assert len(out) == len(picked)
    # several documents at a time: same pages, same order and names
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    settings = type("S", (), {"projects_dir": root, "db_path": db_path})()
    again = export(conn, settings, tmp_path / "typen-parallel", picked, parallel=3)
    assert [e["datei"] for e in again] == [e["datei"] for e in out]
    bare = export(conn, settings, tmp_path / "ohne-text", picked, texts=False)
    assert len(bare) == len(out) and not list((tmp_path / "ohne-text").glob("*.txt"))


def test_max_resolution_keeps_the_aspect_ratio():
    import io

    from projektakte.ocr_testseiten import fit

    def image(w, h, fmt="PNG"):
        buf = io.BytesIO()
        Image.new("RGB", (w, h), "white").save(buf, fmt)
        return buf.getvalue()

    def size(data):
        return Image.open(io.BytesIO(data)).size

    mime, data = fit("image/png", image(3328, 2048), (1664, 2048))  # landscape: still at most 1664 wide
    assert size(data) == (1664, 1024)
    mime, data = fit("image/tiff", image(3000, 4000, "TIFF"), (1664, 2048))  # format kept: same file name
    assert mime == "image/tiff" and size(data) == (1536, 2048)
    mime, data = fit("image/jpeg", image(1754, 2480, "JPEG"), (1664, 2048))  # A4 at 150 dpi: height limits
    assert mime == "image/jpeg" and size(data) == (1448, 2048)
    small = image(800, 600)
    assert fit("image/png", small, (1664, 2048)) == ("image/png", small)  # never enlarged
    assert fit("image/png", small, None) == ("image/png", small)


def test_a_file_pypdf_gives_up_on_is_drawn_by_pdfium(monkeypatch):
    from pypdf.errors import PdfStreamError

    from .test_bildauswertung_stufen import plain_scan as scan

    def broken(_data):
        raise PdfStreamError("Stream has ended unexpectedly")
    monkeypatch.setattr(extract_mod, "_open_pdf", broken)
    pages = extract_mod.page_images(scan(2), [1, 2, 3], "kaputt.pdf")
    assert [bool(p.images) for p in pages] == [True, True, False]
    assert pages[2].problem == "Seite nicht vorhanden"
    assert all(mime == "image/jpeg" for p in pages[:2] for mime, _ in p.images)


def test_a_new_export_clears_pages_numbered_beyond_999(tmp_path):
    from projektakte.ocr_testseiten import _clear

    for name in ("001_a_S1.png", "999_b_S1.jpg", "1000_c_S1.png", "2992_d_S2.jpg", "1000_c_S1.tesseract.txt",
                 "seiten.json", "seitentypen.csv", "notiz.txt"):
        (tmp_path / name).write_text("x")
    assert _clear(tmp_path) == 6
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notiz.txt", "seitentypen.csv"]


def test_renewing_writes_the_same_pages_smaller(tmp_path, monkeypatch):
    import io

    from projektakte.ocr_testseiten import renew

    monkeypatch.setattr(extract_mod, "ocr_available", lambda: False)
    png = tmp_path / "x.png"
    Image.new("RGB", (3000, 2000), "white").save(png)
    conn, root = _setup(tmp_path, {"A/Scan.pdf": plain_scan(2), "Bilder/Foto.png": png.read_bytes()})
    _stage1(conn, root, Vision())
    settings = type("S", (), {"projects_dir": root})()
    picked = _all_candidates(conn, 10, seed=1, per_doc=2)
    out = export(conn, settings, tmp_path / "typen", picked)
    listing = tmp_path / "typen" / "seiten.json"
    old = json.loads(listing.read_text(encoding="utf-8"))
    for e in old:  # an export before 20.11.2026: found by its path
        e.pop("id"), e.pop("pfad")
    listing.write_text(json.dumps(old), encoding="utf-8")
    stats = renew(conn, settings, tmp_path / "typen", max_size=(600, 800))
    assert stats["fehlt"] == stats["mehrdeutig"] == stats["abweichend"] == 0
    new = json.loads(listing.read_text(encoding="utf-8"))
    assert [e["datei"] for e in new] == [e["datei"] for e in out]  # same names, same pages
    assert all(e["id"] for e in new)
    for e in new:
        w, h = Image.open(io.BytesIO((tmp_path / "typen" / e["datei"]).read_bytes())).size
        assert w <= 600 and h <= 800
