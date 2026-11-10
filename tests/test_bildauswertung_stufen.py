"""Two stages for scans: a file is stored at once with its text layer or Tesseract's text, its scan pages
are read by the model later, in the phase "Bildauswertung" (vision.py) – with the page image and that
text, newest first, a few at a time, within a time budget per pass."""
from __future__ import annotations

import email.message
import io
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from projektakte import db, documents, vision
from projektakte import extract as extract_mod
from projektakte.config import load_settings
from projektakte.documents import index_all_documents
from projektakte.extract import VISION_PROMPT, extract
from projektakte.mails.eml_source import sync_eml_folder
from projektakte.registry import sync_from_folder
from projektakte.vision import VisionPass

from .test_extract_r5 import Doc, draw, jpeg, scan

LAYER = "Pruefprotokoll Brandschutzklappe BSK 12 Wartung bestanden"
HANDWRITING = "[handschriftlich: erledigt 12.03.]"


# -- files ------------------------------------------------------------------------------------------------------
def _text_content(text: str) -> bytes:
    return b"BT /F1 12 Tf 50 780 Td (%s) Tj ET" % text.encode("latin-1")


def _page_with_text(doc: Doc, content: bytes, xobjects: dict[str, int]) -> None:
    font = doc.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    cid = doc.stream(b"", content)
    xo = b" ".join(b"/%s %d 0 R" % (n.encode(), i) for n, i in xobjects.items())
    doc.pages.append((cid, b"<< /Font << /F1 %d 0 R >> /XObject << %s >> >>" % (font, xo)))


def ocr_layer_scan(text: str = LAYER) -> bytes:
    """A scanned page with an invisible OCR text layer: a page image of 1.2 MP covering the whole page."""
    doc = Doc()
    img = doc.image(jpeg(scan((1200, 1000), text="Pruefprotokoll")), (1200, 1000), filt=b"/DCTDecode")
    _page_with_text(doc, draw("Im1", (595, 0, 0, 842, 0, 0)) + b"\n" + _text_content(text), {"Im1": img})
    return doc.build()


def digital_with_logo() -> bytes:
    """A digital page: a text layer and a small logo."""
    doc = Doc()
    logo = doc.image(jpeg(scan((200, 100), text="Logo")), (200, 100), filt=b"/DCTDecode")
    _page_with_text(doc, draw("Im1", (100, 0, 0, 50, 450, 770)) + b"\n" + _text_content(LAYER), {"Im1": logo})
    return doc.build()


def plain_scan(pages: int = 1) -> bytes:
    """Scanned pages without a text layer."""
    doc = Doc()
    for k in range(pages):
        img = doc.image(jpeg(scan((600, 800), text=f"Seite {k + 1}")), (600, 800), filt=b"/DCTDecode")
        doc.page(draw("Im1", (595, 0, 0, 842, 0, 0)), {"Im1": img})
    return doc.build()


def png(text: str = "Skizze") -> bytes:
    out = io.BytesIO()
    scan((300, 200), text=text).convert("RGB").save(out, "PNG")
    return out.getvalue()


# -- models and Tesseract -----------------------------------------------------------------------------------
class Vision:
    """A vision model that records what it gets; ``delay`` makes every call take that long."""
    vision, model = True, "bildmodell"

    def __init__(self, answer: str = f"Pruefprotokoll BSK 12 Wartung bestanden {HANDWRITING}", delay: float = 0.0,
                 clock=None, fail: bool = False):
        self.answer, self.delay, self.clock, self.fail = answer, delay, clock, fail
        self.prompts: list[str] = []
        self.images: list[bytes] = []
        self.order: list[str] = []
        self.active = self.most = 0
        self._lock = threading.Lock()

    def describe_image(self, data, mime, prompt):
        with self._lock:
            self.active += 1
            self.most = max(self.most, self.active)
            self.prompts.append(prompt)
            self.images.append(data)
        try:
            Image.open(io.BytesIO(data)).load()  # a real image
            if self.delay:
                time.sleep(self.delay)
            if self.clock is not None:
                self.clock.t += 100
            if self.fail:
                raise RuntimeError("Server nicht erreichbar")
            return type("R", (), {"text": self.answer})()
        finally:
            with self._lock:
                self.active -= 1


class Clock:
    t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def tesseract(monkeypatch):
    """Tesseract installed (faked): every page image gives this text."""
    calls: list[bytes] = []

    def fake(data: bytes) -> str:
        calls.append(data)
        return "Seite gescannt Rauchabzug Treppenraum erkannt"

    monkeypatch.setattr(extract_mod, "ocr_available", lambda: True)
    monkeypatch.setattr(extract_mod, "ocr_image", fake)
    return calls


# -- the index ----------------------------------------------------------------------------------------------
def _setup(tmp_path, files: dict[str, bytes], mtimes: dict[str, float] | None = None):
    root = tmp_path / "projekte"
    folder = root / "BS-2024-0001 Halle"
    folder.mkdir(parents=True)
    for name, data in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mtimes and name in mtimes:
            os.utime(path, (mtimes[name], mtimes[name]))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_from_folder(conn, root, r"BS-\d{4}-\d{4}")
    conn.commit()
    return conn, root


def _stage1(conn, root, llm):
    with documents.vision_later():
        return index_all_documents(conn, llm, projects_dir=root)


def _docs(conn) -> dict:
    return {r["rel_path"]: dict(r) for r in conn.execute("SELECT * FROM documents")}


def _text(conn, name: str) -> str:
    return "\n".join(r[0] for r in conn.execute(
        "SELECT c.text FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE d.rel_path = ? ORDER BY c.ord", (name,)))


def _fts(conn, word: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (word,)).fetchone()[0]


# -- stage 1 and 2 ------------------------------------------------------------------------------------------
def test_scan_with_ocr_layer_goes_to_the_model_with_image_and_text(tmp_path):
    conn, root = _setup(tmp_path, {"Protokoll.pdf": ocr_layer_scan()})
    llm = Vision()
    _stage1(conn, root, llm)
    doc = _docs(conn)["Protokoll.pdf"]
    # stage 1: stored at once with its OCR layer, searchable, the page waits for the model
    assert llm.prompts == []
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("ok", 1, 1)
    assert "Bildauswertung ausstehend: 1 Seite(n)" in doc["message"]
    assert LAYER in _text(conn, "Protokoll.pdf") and _fts(conn, "Brandschutzklappe") == 1
    stats = VisionPass(conn, llm, projects_dir=root).run()
    # stage 2: the model got the page image and the layer text; its answer with the handwriting replaces it
    assert len(llm.prompts) == 1 and LAYER in llm.prompts[0] and "Erkannter Text" in llm.prompts[0]
    assert "[handschriftlich: …]" in llm.prompts[0] and "[Stempel: …]" in llm.prompts[0]
    assert Image.open(io.BytesIO(llm.images[0])).size[0] >= 1000
    doc = _docs(conn)["Protokoll.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"], doc["method"], doc["message"]) == (
        "ok", 0, 0, "vision", None)
    assert HANDWRITING in _text(conn, "Protokoll.pdf") and _fts(conn, "handschriftlich") == 1
    assert (stats["pages"], stats["documents"], stats["pending_pages"]) == (1, 1, 0)
    assert conn.execute("SELECT COUNT(*) FROM page_texts").fetchone()[0] == 0  # dropped once done
    # nothing more to do: the next pass reads nothing again
    _stage1(conn, root, llm)
    VisionPass(conn, llm, projects_dir=root).run()
    assert len(llm.prompts) == 1


def test_scan_without_text_gets_tesseract_text_first_then_the_models(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    llm = Vision(answer="Rauchabzug Treppenraum geprueft [Stempel: Bauaufsicht]")
    _stage1(conn, root, llm)
    doc = _docs(conn)["Scan.pdf"]
    assert len(tesseract) == 1 and llm.prompts == []
    assert (doc["status"], doc["vision_pending"]) == ("ok", 1)
    assert doc["message"] == "Bildauswertung ausstehend: 1 Seite(n)"
    assert "Rauchabzug Treppenraum erkannt" in _text(conn, "Scan.pdf")
    VisionPass(conn, llm, projects_dir=root).run()
    assert "Rauchabzug Treppenraum erkannt" in llm.prompts[0]  # Tesseract's text goes to the model
    assert _text(conn, "Scan.pdf") == "Rauchabzug Treppenraum geprueft [Stempel: Bauaufsicht]"
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["incomplete"], doc["vision_pending"], doc["method"]) == (0, 0, "vision")
    assert doc["message"] is None


def test_digital_pages_never_go_to_the_model(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Bericht.pdf": digital_with_logo()})
    llm = Vision()
    _stage1(conn, root, llm)
    VisionPass(conn, llm, projects_dir=root).run()
    doc = _docs(conn)["Bericht.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"], doc["method"]) == ("ok", 0, 0, "text")
    assert llm.prompts == [] and tesseract == []
    # also when read in one go
    r = extract(Path("Bericht.pdf"), digital_with_logo(), llm=llm)
    assert (r.status, r.incomplete, r.vision_pages, r.method) == ("ok", False, [], "text") and llm.prompts == []


def test_read_in_one_go_the_model_also_gets_the_ocr_layer():
    llm = Vision()
    r = extract(Path("Protokoll.pdf"), ocr_layer_scan(), llm=llm)
    assert (r.status, r.method, r.incomplete) == ("ok", "vision", False)
    assert LAYER in llm.prompts[0] and HANDWRITING in r.text and LAYER not in r.text
    # the model fails: the text layer stands, retried later
    r = extract(Path("Protokoll.pdf"), ocr_layer_scan(), llm=Vision(fail=True))
    assert (r.status, r.incomplete, r.retry) == ("ok", True, True) and LAYER in r.text


def test_at_most_vision_concurrency_calls_at_a_time(tmp_path):
    files = {f"Skizze{k}.png": png(f"Skizze {k}") for k in range(7)}
    for concurrency in (3, 1):
        conn, root = _setup(tmp_path / str(concurrency), files)
        llm = Vision(delay=0.15)
        _stage1(conn, root, llm)
        assert vision.pending_counts(conn)["pages"] == 7
        stats = VisionPass(conn, llm, concurrency=concurrency, projects_dir=root).run()
        assert stats["pages"] == 7 and len(llm.prompts) == 7
        assert llm.most == concurrency, concurrency
        assert all(d["vision_pending"] == 0 and d["status"] == "ok" for d in _docs(conn).values())
        conn.close()


def test_the_time_budget_ends_the_pass_and_the_next_goes_on(tmp_path):
    conn, root = _setup(tmp_path, {"Akte.pdf": plain_scan(pages=5)})
    clock = Clock()
    llm = Vision(clock=clock)  # every answer takes 100 s
    _stage1(conn, root, llm)
    stats = VisionPass(conn, llm, seconds=250, concurrency=1, projects_dir=root, clock=clock).run()
    assert (stats["pages"], stats["stopped"], stats["pending_pages"], stats["pending_documents"]) == (3, True, 2, 1)
    doc = _docs(conn)["Akte.pdf"]
    # the pages read so far are searchable already, the rest waits
    assert doc["vision_pending"] == 2 and "Bildauswertung ausstehend: 2 Seite(n)" in doc["message"]
    assert _text(conn, "Akte.pdf").count(HANDWRITING) == 3
    states = [r[0] for r in conn.execute("SELECT state FROM page_texts ORDER BY page")]
    assert states == ["done", "done", "done", "pending", "pending"]
    stats = VisionPass(conn, llm, seconds=250, concurrency=1, projects_dir=root, clock=clock).run()
    assert (stats["pages"], stats["stopped"], stats["pending_pages"]) == (2, False, 0)
    assert len(llm.prompts) == 5 and _text(conn, "Akte.pdf").count(HANDWRITING) == 5
    assert _docs(conn)["Akte.pdf"]["vision_pending"] == 0


def test_newest_documents_first(tmp_path):
    year = 365 * 24 * 3600
    base = time.time() - 10 * year
    files = {"Alt.png": png("Alt"), "Neu.png": png("Neu"), "Mittel.png": png("Mittel")}
    mtimes = {"Alt.png": base, "Mittel.png": base + 3 * year, "Neu.png": base + 6 * year}
    conn, root = _setup(tmp_path, files, mtimes)
    seen = []

    class Recording(Vision):
        def describe_image(self, data, mime, prompt):
            seen.append(Image.open(io.BytesIO(data)).size)
            return super().describe_image(data, mime, prompt)

    llm = Recording()
    _stage1(conn, root, llm)
    ids = {r["rel_path"]: r["id"] for r in _docs(conn).values()}
    order = []
    real = VisionPass._store

    def store(self, doc_id, page, **kw):
        order.append(doc_id)
        return real(self, doc_id, page, **kw)

    VisionPass._store = store
    try:
        VisionPass(conn, llm, concurrency=1, projects_dir=root).run()
    finally:
        VisionPass._store = real
    assert order == [ids["Neu.png"], ids["Mittel.png"], ids["Alt.png"]]


def test_status_page_shows_what_waits(tmp_path, fake_ai):
    from .conftest import fake_settings
    from projektakte.main import create_app

    settings = fake_settings(fake_ai, tmp_path / "data")
    conn, root = _setup(tmp_path, {"A.pdf": plain_scan(pages=2), "B.png": png()})
    _stage1(conn, root, Vision())
    assert vision.pending_counts(conn) == {"pages": 3, "documents": 2, "recheck": 0}
    conn.close()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    settings.db_path.write_bytes((tmp_path / "x.sqlite3").read_bytes())
    with TestClient(create_app(settings, background=False)) as client:
        counts = client.get("/api/status").json()["counts"]
        assert (counts["vision_pending_pages"], counts["vision_pending_documents"]) == (3, 2)
        page = client.get("/status").text
        assert "Bildauswertung" in page and "3 Seiten in 2 Dokumenten ausstehend" in page


def test_missing_tesseract_falls_back_to_the_model_alone(tmp_path):
    assert extract_mod.ocr_available() is False and extract_mod.ocr_image(b"x") == ""
    assert extract_mod.extraction_profile(None) == f"v{extract_mod.EXTRACT_VERSION}:text"
    # LLM_VISION=0 and no Tesseract: as before
    r = extract(Path("Scan.pdf"), plain_scan())
    assert (r.status, r.incomplete, r.message) == (
        "skipped", True, "Scan ohne Textebene – Bildauswertung ausgeschaltet (LLM_VISION=0)")
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    llm = Vision()
    _stage1(conn, root, llm)
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["status"], doc["vision_pending"], doc["message"]) == ("skipped", 1, "Bildauswertung ausstehend: 1 Seite(n)")
    VisionPass(conn, llm, projects_dir=root).run()
    assert llm.prompts == [VISION_PROMPT]  # no text to hand over
    assert _docs(conn)["Scan.pdf"]["status"] == "ok" and HANDWRITING in _text(conn, "Scan.pdf")


def test_without_vision_tesseract_still_reads_scans(tmp_path, tesseract):
    r = extract(Path("Scan.pdf"), plain_scan())
    assert (r.status, r.incomplete, r.vision_pages) == ("ok", True, [])
    assert "Rauchabzug Treppenraum erkannt" in r.text and "Texterkennung" in r.message
    assert extract_mod.extraction_profile(None) == f"v{extract_mod.EXTRACT_VERSION}:text+ocr"
    r = extract(Path("Protokoll.pdf"), ocr_layer_scan())  # the OCR layer stands; incomplete until vision is on
    assert (r.status, r.incomplete) == ("ok", True) and LAYER in r.text and len(tesseract) == 1


def test_tesseract_runs_as_a_program(tmp_path, monkeypatch):
    """The real call: a program named like Tesseract that lists German and reads the image from stdin."""
    program = tmp_path / "tesseract"
    program.write_text("#!/bin/sh\nif [ \"$1\" = \"--list-langs\" ]; then echo 'List of available languages (2):';"
                       " echo deu; echo eng; exit 0; fi\ncat > /dev/null\necho 'Rettungsweg  Treppenraum Nord'\n"
                       "printf '\\f'\n", encoding="utf-8")
    program.chmod(program.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(extract_mod, "OCR_COMMAND", str(program))
    assert extract_mod.ocr_available() is True
    assert extract_mod.ocr_image(png()) == "Rettungsweg  Treppenraum Nord"
    assert extract_mod.clean_ocr("a b\n\n\n\nc") == ""  # stray letters are no text
    r = extract(Path("Scan.pdf"), plain_scan())  # LLM_VISION=0: Tesseract's text
    assert r.text == "Rettungsweg  Treppenraum Nord"


def test_a_failed_model_call_is_tried_again_later(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan()})
    _stage1(conn, root, Vision())
    stats = VisionPass(conn, Vision(fail=True), projects_dir=root).run()
    assert (stats["pages"], stats["failed"], stats["pending_pages"]) == (0, 1, 1)
    doc = _docs(conn)["Scan.pdf"]
    assert doc["vision_pending"] == 1 and "Rauchabzug Treppenraum erkannt" in _text(conn, "Scan.pdf")
    llm = Vision()
    VisionPass(conn, llm, projects_dir=root).run()
    assert llm.prompts == []  # not before its retry time
    conn.execute("UPDATE page_texts SET retry_after = '2000-01-01 00:00:00'")
    conn.commit()
    VisionPass(conn, llm, projects_dir=root).run()
    assert len(llm.prompts) == 1 and _docs(conn)["Scan.pdf"]["vision_pending"] == 0


def test_older_complete_pdfs_are_checked_for_scan_pages(tmp_path):
    conn, root = _setup(tmp_path, {"Protokoll.pdf": ocr_layer_scan(), "Bericht.pdf": digital_with_logo()})
    index_all_documents(conn, None, projects_dir=root)
    # as stored by version 6 without vision: complete, the OCR layer as its text
    conn.execute("UPDATE documents SET incomplete = 0, message = NULL, extract_profile = 'v6:text'")
    conn.commit()
    assert vision.pending_counts(conn)["recheck"] == 2
    llm = Vision()
    stats = VisionPass(conn, llm, projects_dir=root).run()
    assert (stats["checked"], stats["found"], stats["pages"]) == (2, 1, 1)
    docs = _docs(conn)
    assert HANDWRITING in _text(conn, "Protokoll.pdf") and docs["Protokoll.pdf"]["vision_pending"] == 0
    assert docs["Bericht.pdf"]["extract_profile"] == extract_mod.extraction_profile(llm)  # checked, unchanged
    assert docs["Bericht.pdf"]["method"] == "text" and len(llm.prompts) == 1
    assert vision.pending_counts(conn)["recheck"] == 0


def test_mail_attachments_take_the_same_way(tmp_path, tesseract):
    mails = tmp_path / "mails"
    mails.mkdir()
    msg = email.message.EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "Anna <anna@example.org>", "buero@example.org", "Scan Abnahme"
    msg["Date"], msg["Message-ID"] = "Mon, 3 Mar 2025 10:00:00 +0100", "<scan@example.org>"
    msg.set_content("Anbei der Scan.")
    msg.add_attachment(plain_scan(), maintype="application", subtype="pdf", filename="Abnahme.pdf")
    (mails / "scan.eml").write_bytes(msg.as_bytes())
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    llm = Vision()
    with documents.vision_later():
        sync_eml_folder(conn, mails, cache_dir=tmp_path / "cache", llm=llm)
    doc = conn.execute("SELECT * FROM documents WHERE mail_id IS NOT NULL").fetchone()
    assert doc["vision_pending"] == 1 and llm.prompts == [] and len(tesseract) == 1
    VisionPass(conn, llm).run()
    doc = conn.execute("SELECT * FROM documents WHERE mail_id IS NOT NULL").fetchone()
    assert (doc["vision_pending"], doc["status"], doc["doc_date"]) == (0, "ok", "2025-03-03")
    assert HANDWRITING in "".join(r[0] for r in conn.execute("SELECT text FROM chunks WHERE doc_id = ?", (doc["id"],)))
    assert "Rauchabzug Treppenraum erkannt" in llm.prompts[0]


def test_photos_in_picture_folders_get_neither_tesseract_nor_the_model(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Bilder/Foto.png": png()})
    llm = Vision()
    with documents.vision_later():
        index_all_documents(conn, llm, projects_dir=root, skip_photos=("Bilder",))
    VisionPass(conn, llm, projects_dir=root).run()
    doc = _docs(conn)["Bilder/Foto.png"]
    assert (doc["status"], doc["message"], doc["vision_pending"]) == ("skipped", documents.PHOTO_SKIPPED_TEXT, 0)
    assert tesseract == [] and llm.prompts == []


def test_settings(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path)})
    assert (s.vision_concurrency, s.vision_pass_seconds) == (3, 1800.0)
    s = load_settings({"DATA_DIR": str(tmp_path), "VISION_CONCURRENCY": "1", "VISION_PASS_SECONDS": "0"})
    assert (s.vision_concurrency, s.vision_pass_seconds) == (1, 0.0)
    s = load_settings({"DATA_DIR": str(tmp_path), "VISION_CONCURRENCY": "0", "VISION_PASS_SECONDS": "-5"})
    assert (s.vision_concurrency, s.vision_pass_seconds) == (3, 1800.0)
    assert any("VISION_CONCURRENCY" in w for w in s.config_warnings)
    assert any("VISION_PASS_SECONDS" in w for w in s.config_warnings)


def test_run_index_reads_scans_in_the_phase_bildauswertung(tmp_path, fake_ai):
    """The whole pass with the fake services: documents first, the model's text at the end of the pass."""
    from .conftest import fake_settings
    from projektakte.ai.services import AIServices
    from projektakte.indexer import run_index

    root = tmp_path / "projekte" / "BS-2024-0002 Schule"
    root.mkdir(parents=True)
    (root / "Protokoll.pdf").write_bytes(ocr_layer_scan())
    settings = fake_settings(fake_ai, tmp_path / "data", PROJECTS_DIR=tmp_path / "projekte", LLM_VISION=1,
                             MAIL_DIR=tmp_path / "keine-mails")
    phases = []
    stats = run_index(settings, AIServices.from_settings(settings), progress=phases.append)
    assert phases.index("Bildauswertung") > phases.index("Embeddings")
    assert (stats["vision"]["pages"], stats["vision"]["pending_pages"]) == (1, 0)
    assert stats["vision_embeddings"]["embedded"] >= 1
    conn = sqlite3.connect(settings.db_path)
    text = "".join(r[0] for r in conn.execute("SELECT text FROM chunks"))
    assert "Fake-Transkription" in text
    assert conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 0
    conn.close()


@pytest.mark.skipif(not __import__("shutil").which("tesseract"), reason="Tesseract nicht installiert")
def test_real_tesseract_reads_a_scanned_page(monkeypatch):
    """With Tesseract installed (apt install tesseract-ocr tesseract-ocr-deu): a scanned page gets its text."""
    from PIL import ImageDraw, ImageFont

    monkeypatch.setattr(extract_mod, "OCR_COMMAND", "tesseract")
    if not extract_mod.ocr_available():
        pytest.skip("Tesseract ohne deutsche Sprachdaten")
    img = Image.new("L", (1240, 1754), 255)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 36)
    except OSError:
        font = ImageFont.load_default(size=36)
    ImageDraw.Draw(img).text((100, 200), "Rettungsweg über Treppenraum Nord", fill=0, font=font)
    doc = Doc()
    data = doc.image(jpeg(img), img.size, filt=b"/DCTDecode")
    doc.page(draw("Im1", (595, 0, 0, 842, 0, 0)), {"Im1": data})
    r = extract(Path("Scan.pdf"), doc.build())
    assert "Treppenraum" in r.text and r.incomplete
