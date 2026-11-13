"""PDF pages without a text layer and without images – text drawn as outlines (printed codes, CAD plans),
fonts without a usable character map – are rendered (PDFium) and read like scan pages: Tesseract first,
then the model with the rendered page. Blank pages stay blank; without pypdfium2 nothing changes."""
from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from projektakte import db
from projektakte import extract as extract_mod
from projektakte.extract import extract

from .test_bildauswertung_stufen import Vision
from .test_extract_r5 import Doc

pytest.importorskip("pypdfium2")


def glyphs() -> bytes:
    """Filled outline paths shaped like letters in lines of text – no font, no image."""
    out = [b"0 0 0 rg"]
    for row in range(30):
        y = 780 - row * 22
        for col in range(40):
            x = 50 + col * 12
            out.append(b"%d %d m %d %d l %d %d l %d %d l h f" % (x, y, x + 4, y + 12, x + 8, y, x + 4, y + 5))
    return b"\n".join(out)


def outlined_and_blank() -> bytes:
    doc = Doc()
    doc.page(glyphs(), {}, compress=True)
    doc.page(b"", {})  # blank: nothing drawn
    doc.page(b"1 1 1 rg 0 0 595 842 re f", {})  # blank: white drawn on white
    return doc.build()


@pytest.fixture()
def tesseract(monkeypatch):
    calls: list[bytes] = []

    def fake(data: bytes) -> str:
        calls.append(data)
        return "Stellplätze Garagen verkehrssicher Abstellplätze"

    monkeypatch.setattr(extract_mod, "ocr_image", fake)
    return calls


def test_an_outlined_page_goes_to_tesseract_and_the_model_as_a_rendered_image(tesseract):
    llm = Vision(answer="Stellplätze und Garagen müssen verkehrssicher sein")
    r = extract(Path("Auszug.pdf"), outlined_and_blank(), llm=llm, ocr=True)
    assert (r.status, r.incomplete, r.method) == ("ok", False, "vision")
    assert [p.number for p in r.pages] == [1]  # the blank pages stay blank
    assert len(tesseract) == 1 and len(llm.images) == 1  # one rendered page for both
    with Image.open(io.BytesIO(tesseract[0])) as img:
        assert img.size == (2479, 3508) and img.mode == "L"  # Tesseract: A4 at 300 dpi, grey
    with Image.open(io.BytesIO(llm.images[0])) as img:
        assert img.size == (1239, 1754) and img.mode == "L"  # the model: at VISION_DPI (150), grey
    assert "Stellplätze Garagen" in llm.prompts[0]  # Tesseract's text goes with the image


def test_stage_one_lists_the_outlined_page_and_page_images_renders_it(tesseract):
    data = outlined_and_blank()
    r = extract(Path("Auszug.pdf"), data, llm=Vision(), ocr=True, defer_vision=True)
    assert r.status == "ok" and r.vision_pages == [1] and r.pages[0].text.startswith("Stellplätze")
    first, blank = extract_mod.page_images(data, [1, 2], "Auszug.pdf")
    assert first.problem is None and first.images[0][0] == "image/png"
    assert blank.images == [] and blank.problem == "kein Bild auf der Seite"


def test_a_render_over_the_time_bound_is_not_read(monkeypatch, tesseract):
    monkeypatch.setattr(extract_mod, "RENDER_SECONDS", -1.0)
    r = extract(Path("Plan.pdf"), outlined_and_blank(), llm=Vision(), ocr=True)
    assert r.status == "skipped" and r.incomplete and "Seiteninhalt zu umfangreich" in r.message
    assert tesseract == []


def test_without_pypdfium2_nothing_changes(monkeypatch, tesseract):
    monkeypatch.setattr(extract_mod, "_pdfium", lambda: None)
    llm = Vision()
    r = extract(Path("Auszug.pdf"), outlined_and_blank(), llm=llm, ocr=True)
    assert (r.status, r.message) == ("empty", "kein Text gefunden") and llm.images == [] and tesseract == []


def test_garbled_text_layers_count_as_none():
    assert extract_mod._garbled("��\x01\x02  ab")
    assert not extract_mod._garbled("Tabelle 1 | 2.345,00 | 12,5 % – Brandabschnitt")


def test_migration_reads_empty_and_gappy_pdfs_once_more(tmp_path):
    dbp = tmp_path / "alt.sqlite3"
    conn = sqlite3.connect(dbp)
    for i, script in enumerate(db.MIGRATIONS[:14], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    rows = [(1, "leer.pdf", ".pdf", "empty", None), (2, "luecke.pdf", ".pdf", "ok", 3),
            (3, "voll.pdf", ".pdf", "ok", 2), (4, "leer.docx", ".docx", "empty", None),
            (5, "anhang.pdf", ".pdf", "empty", None)]
    for doc_id, name, ext, status, pages in rows:
        conn.execute("INSERT INTO documents(id, path, rel_path, filename, ext, status, pages, extract_profile, "
                     "mail_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'p', ?)",
                     (doc_id, f"/x/{name}", name, name, ext, status, pages, 7 if doc_id == 5 else None))
    for doc_id, page in ((2, 1), (2, 3), (3, 1), (3, 2)):
        conn.execute("INSERT INTO chunks(doc_id, ord, page, text) VALUES (?, 0, ?, 't')", (doc_id, page))
    conn.commit()
    conn.close()
    db.init_db(dbp)
    with db.session(dbp) as conn:
        marked = conn.execute("SELECT id FROM documents WHERE incomplete = 1 AND extract_profile = '' "
                              "ORDER BY id").fetchall()
    assert [r[0] for r in marked] == [1, 2, 5]
