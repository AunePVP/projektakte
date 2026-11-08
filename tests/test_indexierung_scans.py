"""Scanned PDFs with LLM_VISION=1: real image PDFs (JPEG, Flate/PNG, CCITT, JBIG2, broken images) are
read by the multimodal model; images that cannot be decoded make the page fail and the document is
retried later – never recorded as "no text"."""
import io
import shutil
import zlib
from datetime import timedelta
from pathlib import Path

import pypdf.generic._image_xobject as pypdf_images
import pytest
from PIL import Image, ImageDraw

from projektakte import db, documents
from projektakte import extract as extract_mod
from projektakte.documents import index_all_documents
from projektakte.extract import extract, extraction_profile
from projektakte.registry import scan_projects, sync_projects

PATTERN = r"BS-\d{4}-\d{4}"


# -- PDFs built from real images ----------------------------------------------------------------------
def page_image(mode: str = "RGB", size=(420, 594), text: str = "AUFLAGE 7 RAUCHABZUG") -> Image.Image:
    img = Image.new("RGB", size, "white")
    ImageDraw.Draw(img).text((24, 24), text, fill="black")
    return img.convert(mode)


def pillow_scan_pdf(*images: Image.Image) -> bytes:
    """A PDF as a scanner writes it: one image per page, no text layer (Pillow encodes RGB/grey as
    JPEG, bilevel as CCITT fax)."""
    buf = io.BytesIO()
    images[0].save(buf, "PDF", resolution=72, save_all=True, append_images=list(images[1:]))
    return buf.getvalue()


def jpeg_xobject(text: str = "PLAN EG") -> dict:
    buf = io.BytesIO()
    img = page_image("RGB", (300, 200), text)
    img.save(buf, "JPEG", quality=85)
    return {"data": buf.getvalue(), "filter": "/DCTDecode", "size": img.size, "colorspace": "/DeviceRGB", "bpc": 8}


def flate_xobject() -> dict:
    """Grey pixels, Flate-compressed – pypdf hands them out as PNG."""
    img = page_image("L", (240, 160), "BRANDWAND")
    return {"data": zlib.compress(img.tobytes()), "filter": "/FlateDecode", "size": img.size,
            "colorspace": "/DeviceGray", "bpc": 8}


def jbig2_xobject() -> dict:
    """JBIG2 scan: pypdf needs the jbig2dec program for it (not installed in the image)."""
    return {"data": b"\x97JB2\r\n\x1a\n\x01" + bytes(64), "filter": "/JBIG2Decode", "size": (400, 560),
            "colorspace": "/DeviceGray", "bpc": 1}


def broken_jpeg_xobject() -> dict:
    return {"data": b"\xff\xd8 kein echtes JPEG", "filter": "/DCTDecode", "size": (100, 100),
            "colorspace": "/DeviceRGB", "bpc": 8}


def build_pdf(pages: list) -> bytes:
    """Minimal PDF. Each page is a text (text layer) or a list of image XObjects ([] = blank page)."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    def stream(head: bytes, data: bytes) -> bytes:
        return b"<< " + head + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    parts = []
    for page in pages:
        if isinstance(page, str):
            text = page.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
            content = add(stream(b"", b"BT /F1 11 Tf 50 780 Td (" + text + b") Tj ET"))
            parts.append((content, b"<< /Font << /F1 %d 0 R >> >>" % font))
            continue
        names, ops = [], []
        for n, x in enumerate(page, start=1):
            head = (b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent %d "
                    b"/Filter %s" % (x["size"][0], x["size"][1], x["colorspace"].encode(), x["bpc"],
                                     x["filter"].encode()))
            names.append(b"/Im%d %d 0 R" % (n, add(stream(head, x["data"]))))
            ops.append(b"q 400 0 0 300 100 %d cm /Im%d Do Q" % (100 + 320 * (n - 1), n))
        content = add(stream(b"", b"\n".join(ops)))
        parts.append((content, b"<< /XObject << " + b" ".join(names) + b" >> >>"))
    pages_id = len(objects) + len(parts) + 1
    kids = [add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources %s >>"
                % (pages_id, content, res)) for content, res in parts]
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>"
               % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    return bytes(out)


TEXT_PAGE = "Seite eins mit Textebene: Rauchableitung im Treppenraum nach Auflage 4."


class Vision:
    """Multimodal model stand-in that checks it gets real, decodable JPEG/PNG images."""
    vision = True

    def __init__(self, answers=(), model="vision-a"):
        self.model = model
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def describe_image(self, data, mime, prompt):
        img = Image.open(io.BytesIO(data))
        img.load()
        assert mime == {"JPEG": "image/jpeg", "PNG": "image/png"}[img.format]
        self.calls.append((mime, img.mode, img.size))
        answer = self.answers.pop(0) if self.answers else "Auflage 7: Rauchabzug im Treppenraum"
        if isinstance(answer, Exception):
            raise answer

        class Result:
            text = answer
        return Result()


# -- extraction ---------------------------------------------------------------------------------------
def test_scanner_pdf_with_jpeg_and_ccitt_pages_is_read():
    data = pillow_scan_pdf(page_image("RGB"), page_image("1"), page_image("CMYK"))
    llm = Vision()
    r = extract(Path("Scan_Auflagen.pdf"), data, llm=llm)
    assert (r.status, r.method, r.incomplete, r.retry, r.message) == ("ok", "vision", False, False, None)
    assert [p.number for p in r.pages] == [1, 2, 3] and "Rauchabzug" in r.text
    # JPEG goes as it is, the CCITT fax page (TIFF) as PNG, CMYK is converted for the model
    assert [c[:2] for c in llm.calls] == [("image/jpeg", "RGB"), ("image/png", "1"), ("image/jpeg", "RGB")]
    without = extract(Path("Scan_Auflagen.pdf"), data)
    assert (without.status, without.incomplete) == ("skipped", True) and "LLM_VISION=0" in without.message


def test_mixed_pdf_reads_text_layer_and_png_and_jpeg_images():
    data = build_pdf([TEXT_PAGE, [jpeg_xobject()], [flate_xobject()]])
    llm = Vision(["Grundriss Erdgeschoss", "Brandwand F90"])
    r = extract(Path("Gemischt.pdf"), data, llm=llm)
    assert (r.status, r.method, r.incomplete) == ("ok", "vision", False)
    assert [p.number for p in r.pages] == [1, 2, 3]
    assert "Grundriss" in r.pages[1].text and "Brandwand" in r.pages[2].text
    assert [c[0] for c in llm.calls] == ["image/jpeg", "image/png"]


def test_undecodable_images_fail_the_page_and_are_retried():
    """JBIG2 without jbig2dec, a broken JPEG: the model is never asked, so this is no "no text"."""
    llm = Vision()
    r = extract(Path("Scan_Zufahrt.pdf"), build_pdf([[jbig2_xobject()], [broken_jpeg_xobject()]]), llm=llm)
    assert (r.status, r.incomplete, r.retry) == ("error", True, True)
    if shutil.which("jbig2dec") is None:  # as in the Docker image
        assert r.message == "Scan ohne Textebene – Bilder nicht lesbar: JBIG2-Bild, Programm jbig2dec nicht installiert"
    assert "Bilder nicht lesbar" in r.message and "keinen Text" not in r.message
    assert llm.calls == []
    broken = extract(Path("x.pdf"), build_pdf([[broken_jpeg_xobject()]]), llm=llm)
    assert broken.message == "Scan ohne Textebene – Bilder nicht lesbar: Bilddaten beschädigt oder Format unbekannt"
    # next to readable pages: the document stays incomplete and is retried
    mixed = extract(Path("x.pdf"), build_pdf([TEXT_PAGE, [jbig2_xobject()], [jpeg_xobject()]]), llm=Vision())
    assert (mixed.status, mixed.incomplete, mixed.retry) == ("ok", True, True)
    assert [p.number for p in mixed.pages] == [1, 3]
    assert mixed.message.startswith("1 Seite(n) ohne Textebene nicht gelesen (Bilder nicht lesbar")


def test_one_broken_image_does_not_spoil_the_others_on_the_page():
    llm = Vision()
    r = extract(Path("x.pdf"), build_pdf([[broken_jpeg_xobject(), jpeg_xobject()]]), llm=llm)
    assert (r.status, r.method) == ("ok", "vision") and len(llm.calls) == 1


def test_missing_pillow_is_a_retryable_failure(monkeypatch):
    def no_pillow(*args, **kwargs):
        raise ImportError("pillow is required to do image extraction. It can be installed via "
                          "'pip install pypdf[image]'")

    monkeypatch.setattr(pypdf_images, "_xobj_to_image", no_pillow)
    llm = Vision()
    r = extract(Path("scan.pdf"), pillow_scan_pdf(page_image("RGB")), llm=llm)
    assert (r.status, r.incomplete, r.retry) == ("error", True, True)
    assert r.message == "Scan ohne Textebene – Bilder nicht lesbar: Pillow nicht installiert"
    assert llm.calls == []


def test_each_unread_page_is_reported_with_its_reason():
    data = build_pdf([TEXT_PAGE, [jpeg_xobject()], [jpeg_xobject()], [], [jpeg_xobject()]])
    r = extract(Path("x.pdf"), data, llm=Vision(["Grundriss", RuntimeError("timeout"), " "]))
    assert [p.number for p in r.pages] == [1, 2]  # page 4 is blank: nothing to read
    assert (r.incomplete, r.retry) == (True, True)
    assert r.message == ("2 Seite(n) ohne Textebene nicht gelesen "
                         "(1× Bildauswertung fehlgeschlagen; 1× Bildauswertung ergab keinen Text)")
    # only an empty model answer: not retried with the same model (another profile may read it)
    empty = extract(Path("x.pdf"), build_pdf([[jpeg_xobject()]]), llm=Vision([""]))
    assert (empty.status, empty.incomplete, empty.retry) == ("skipped", True, False)
    assert empty.message == "Scan ohne Textebene – Bildauswertung ergab keinen Text"


def test_very_large_page_images_are_scaled_down(monkeypatch):
    monkeypatch.setattr(extract_mod, "MAX_IMAGE_PIXELS", 100 * 100)
    llm = Vision()
    assert extract(Path("plan.pdf"), pillow_scan_pdf(page_image("RGB", (400, 300))), llm=llm).status == "ok"
    (_mime, _mode, (w, h)), = llm.calls
    assert w * h <= 100 * 100 and abs(w / h - 4 / 3) < 0.05


# -- indexing: retried after the cause is fixed, version 1 results are read again ----------------------
@pytest.fixture()
def scan_project(tmp_path):
    root = tmp_path / "projekte"
    proj = root / "BS-2024-0001 Testhalle"
    proj.mkdir(parents=True)
    (proj / "Scan_Auflagen.pdf").write_bytes(pillow_scan_pdf(page_image("RGB")))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_projects(conn, scan_projects(root, PATTERN))
    conn.commit()
    yield conn, root
    conn.close()


def _scan_row(conn):
    return conn.execute("SELECT status, message, method, incomplete, attempts, retry_after, extract_profile "
                        "FROM documents WHERE filename = 'Scan_Auflagen.pdf'").fetchone()


def test_scan_unreadable_without_pillow_is_read_once_it_is_installed(scan_project, monkeypatch):
    conn, root = scan_project
    t0 = documents._utcnow()
    monkeypatch.setattr(documents, "_utcnow", lambda: t0)
    with monkeypatch.context() as m:
        m.setattr(pypdf_images, "_xobj_to_image", lambda *a, **k: (_ for _ in ()).throw(
            ImportError("pillow is required to do image extraction.")))
        index_all_documents(conn, llm=Vision(), projects_dir=root)
    row = _scan_row(conn)
    assert (row["status"], row["incomplete"], row["attempts"]) == ("error", 1, 1)
    assert "Pillow nicht installiert" in row["message"]
    assert row["retry_after"] == documents._sqlite_time(t0 + timedelta(minutes=15))

    llm = Vision()
    monkeypatch.setattr(documents, "_utcnow", lambda: t0 + timedelta(minutes=16))
    assert index_all_documents(conn, llm=llm, projects_dir=root)["reprocessed"] == 1
    row = _scan_row(conn)
    assert (row["status"], row["method"], row["incomplete"], row["retry_after"]) == ("ok", "vision", 0, None)
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'rauchabzug'").fetchone()[0] == 1


def test_scans_stored_by_extraction_version_1_are_read_again(scan_project):
    """Version 1 recorded undecodable scans as "ergab keinen Text" without a retry time."""
    conn, root = scan_project
    llm = Vision()
    index_all_documents(conn, llm=llm, projects_dir=root)
    conn.execute("DELETE FROM chunks")
    conn.execute("UPDATE documents SET status = 'skipped', method = 'text', incomplete = 1, retry_after = NULL, "
                 "message = 'Scan ohne Textebene – Bildauswertung ergab keinen Text', extract_profile = ?",
                 ("v1:vision:" + llm.model,))
    conn.commit()
    assert extraction_profile(llm) != "v1:vision:" + llm.model
    assert index_all_documents(conn, llm=llm, projects_dir=root)["reprocessed"] == 1
    assert (_scan_row(conn)["status"], _scan_row(conn)["incomplete"]) == ("ok", 0)
