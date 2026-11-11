"""Large plan scans: pypdf may decode up to PDF_IMAGE_BYTES per image (its own default, 75 MB, refused colour
scans above 25 MP – an A1 plan at 300 dpi in colour has 70 MP). Documents read under the old limit are read
once more (migration 12); a reason an image cannot be read is logged once per document."""
from __future__ import annotations

import logging
import zlib

from projektakte import db
from projektakte import extract as extract_mod
from projektakte.vision import VisionPass

from .test_bildauswertung_stufen import Vision, _docs, _setup, _stage1, tesseract  # noqa: F401 – fixture
from .test_extract_r5 import Doc, draw, grid

TOO_BIG = "zu groß zum Lesen (Speicher- oder Datengrenze überschritten)"


def colour_plan(size=(5000, 6000)) -> bytes:
    """A colour scan without a text layer whose raw samples (90 MB) are beyond pypdf's 75 MB default."""
    w, h = size
    row = bytes(range(256)) * (w * 3 // 256) + bytes(w * 3 % 256)
    doc = Doc()
    img = doc.image(zlib.compress(row * h, 1), size, cs=b"/DeviceRGB")
    doc.page(draw("Im1", (595, 0, 0, 842, 0, 0)), {"Im1": img})
    return doc.build()


def test_a_colour_plan_beyond_75_mb_is_read(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Plan.pdf": colour_plan()})
    llm = Vision(answer="Grundriss EG Rettungsweg Treppenraum T1")
    _stage1(conn, root, llm)
    doc = _docs(conn)["Plan.pdf"]
    assert len(tesseract) == 1 and doc["vision_pending"] == 1
    VisionPass(conn, llm, projects_dir=root).run()
    doc = _docs(conn)["Plan.pdf"]
    assert len(llm.images) == 1
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("ok", 0, 0)
    assert TOO_BIG not in (doc["message"] or "")


def test_documents_too_big_for_the_old_limit_are_read_again_once(tmp_path, tesseract, monkeypatch):
    monkeypatch.setattr(extract_mod, "PDF_IMAGE_BYTES", 75_000_000)  # the old limit
    conn, root = _setup(tmp_path, {"Plan.pdf": colour_plan()})
    llm = Vision()
    _stage1(conn, root, llm)
    VisionPass(conn, llm, projects_dir=root).run()
    doc = _docs(conn)["Plan.pdf"]
    assert doc["incomplete"] == 1 and TOO_BIG in doc["message"] and llm.images == []
    _stage1(conn, root, llm)
    assert _docs(conn)["Plan.pdf"]["extract_profile"] == doc["extract_profile"]  # not read again by itself

    monkeypatch.setattr(extract_mod, "PDF_IMAGE_BYTES", 450_000_000)
    dbp = tmp_path / "x.sqlite3"
    conn.execute("PRAGMA user_version = 11")
    conn.commit()
    # migrations 12 and 13 once more (the later ones add columns, which exist already)
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS[:13])
    db.init_db(dbp)  # migration 12
    monkeypatch.undo()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    conn.execute(f"PRAGMA user_version = {len(db.MIGRATIONS)}")
    conn.commit()
    monkeypatch.setattr(extract_mod, "PDF_IMAGE_BYTES", 450_000_000)
    assert _docs(conn)["Plan.pdf"]["extract_profile"] == ""
    _stage1(conn, root, llm)
    VisionPass(conn, llm, projects_dir=root).run()
    doc = _docs(conn)["Plan.pdf"]
    assert len(llm.images) == 1 and (doc["incomplete"], doc["vision_pending"]) == (0, 0)


def test_an_unreadable_image_is_logged_once_per_document_and_cause(caplog, monkeypatch):
    """A strip scan whose strips all fail the same way (e.g. jbig2dec missing): one warning, not one per strip."""
    def broken(image, target):
        raise RuntimeError("JBIG2-Bild, Programm jbig2dec nicht installiert")

    monkeypatch.setattr(extract_mod, "_decode", broken)
    doc = Doc()
    names = {f"S{k}": doc.image(zlib.compress(bytes([k * 9]) * (400 * 300)), (400, 300)) for k in range(12)}
    doc.page(b"\n".join(draw(n, m) for n, m in zip(names, grid(12, 40))), names)
    with caplog.at_level(logging.WARNING, logger="projektakte.extract"):
        extract_mod.extract(__import__("pathlib").Path("Streifen.pdf"), doc.build(), llm=Vision())
    lines = [r.getMessage() for r in caplog.records if "nicht lesbar" in r.getMessage()]
    assert len(lines) == 1, lines


def test_the_progress_line_names_the_page(caplog, tesseract):
    doc = Doc()
    for k in range(3):
        img = doc.image(zlib.compress(bytes([200]) * (600 * 800)), (600, 800))
        doc.page(draw("Im1", (595, 0, 0, 842, 0, 0)), {"Im1": img})
    with caplog.at_level(logging.INFO, logger="projektakte.extract"):
        extract_mod.extract(__import__("pathlib").Path("Scan.pdf"), doc.build(), llm=None)
    lines = [r.getMessage() for r in caplog.records if "von 3)" in r.getMessage()]
    assert lines == ["Texterkennung: Scan.pdf – Seite 1 (1 von 3)", "Texterkennung: Scan.pdf – Seite 2 (2 von 3)",
                     "Texterkennung: Scan.pdf – Seite 3 (3 von 3)"]
