"""Stage 1 with the page model (VISION_URL): a PDF is read with PDFium alone; every page of a PDF and every image
file is listed for the model. Pages with a real text layer keep their words (and bring their lines with their place
along); a scan has, for now, only the OCR layer it came with. No Tesseract. Without the page model nothing changes."""
from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from projektakte import seitentypen
from projektakte.demo.writers import pdf_bytes
from projektakte.documents import reprocess_due
from projektakte.extract import PAGE_MODEL_MARK, extract, extraction_profile, page_model, profile_key

from .pdfbau import Pdf, line, scan_image, scan_pdf
from .seitenhilfe import OCR, docs, setup_index, stage1, text_of

P1 = "Brandschutzkonzept für die Grundschule Am Lindenhof in Musterstadt, Fassung vom März."
P2 = "Der zweite Rettungsweg führt über die Außentreppe an der Nordseite des Gebäudes."
P3 = "Die Rauchableitung im Treppenraum erfolgt über ein Fenster im obersten Geschoss."


class Big:
    """The LLM beside a page model: it reads no image (``vision`` off) and names the page model."""
    vision, model, page_model = False, "grosses-modell", "nemotron-kmw"

    def describe_image(self, *a, **kw):
        raise AssertionError("das große Modell bekommt kein Bild")


def three_pages() -> bytes:
    return pdf_bytes([P1, "\f", P2, "\f", P3])


def png(size=(300, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, "white").save(out, "PNG")
    return out.getvalue()


def test_page_model_is_named_by_the_llm():
    assert page_model(Big()) == "nemotron-kmw"
    assert page_model(None) == ""
    assert page_model(type("Old", (), {"vision": True, "model": "x"})()) == ""


def test_all_pages_pending_with_page_model():
    r = extract(Path("Konzept.pdf"), three_pages(), llm=Big())
    assert r.status == "ok" and r.incomplete is False
    assert r.vision_pages == [1, 2, 3] and r.layer_pages == [1, 2, 3]
    assert "Außentreppe" in r.pages[1].text
    # the lines of each page with their place on the rendered page (0–1)
    assert sorted(r.segments) == [1, 2, 3]
    x0, y0, x1, y1, text = r.segments[2][0]
    assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1 and text and text in P2


def test_without_page_model_unchanged():
    r = extract(Path("Konzept.pdf"), three_pages(), llm=None)
    assert r.vision_pages == [] and r.layer_pages == [] and r.segments == {}
    assert extract(Path("Konzept.pdf"), three_pages(), llm=None, defer_vision=True).vision_pages == []


def test_scan_with_ocr_layer_is_not_a_layer_page():
    r = extract(Path("Scan.pdf"), scan_pdf(2, layer=OCR), llm=Big())
    assert r.vision_pages == [1, 2] and r.layer_pages == [] and r.segments == {}
    assert r.status == "ok" and OCR in r.pages[0].text  # the OCR layer is its text for now


def test_scan_without_text_waits_without_text():
    r = extract(Path("Scan.pdf"), scan_pdf(2), llm=Big())
    assert (r.status, r.pages, r.vision_pages, r.layer_pages) == ("skipped", [], [1, 2], [])


def test_digital_page_with_logo_is_a_layer_page():
    data = Pdf().page(line(60, 700, P1), image=(scan_image((300, 120), "Logo"), (300, 120), (400, 760, 150, 60))).build()
    r = extract(Path("Bericht.pdf"), data, llm=Big())
    assert r.vision_pages == [1] and r.layer_pages == [1]


def test_image_file_waits_for_the_page_model():
    r = extract(Path("Skizze.png"), png(), llm=Big())
    assert (r.status, r.vision_pages, r.layer_pages) == ("skipped", [1], [])


def test_every_page_goes_to_the_model_also_empty_ones_and_with_skip_cad(monkeypatch):
    """A page without text and image is not known to be empty before it is rendered (text as outlines, CAD plans):
    it is listed. VISION_SKIP_CAD plays no part with the page model."""
    monkeypatch.setenv("VISION_SKIP_CAD", "1")
    r = extract(Path("Auszug.pdf"), Pdf().page(line(60, 700, P1)).page().build(), llm=Big())
    assert r.vision_pages == [1, 2] and r.layer_pages == [1]


def test_words_are_not_torn_apart(tmp_path):
    """Justified text written in several runs: the old reader gave "Dachflächenwass er"."""
    from .test_pdfseiten import A, B, _end
    from .pdfbau import runs

    data = Pdf().page(runs(700, [(70, A), (_end(A, 70, 11, 0.6), B)], extra=b"0.6 Tc")).build()
    assert extract(Path("Bescheid.pdf"), data, llm=Big()).pages[0].text == A + B


def test_broken_pdf_is_an_error_to_retry():
    r = extract(Path("kaputt.pdf"), b"%PDF-1.4\nkein PDF", llm=Big())
    assert (r.status, r.incomplete, r.retry) == ("error", True, True)


def test_profile_marks_page_model():
    profile = extraction_profile(Big(), ocr=False)
    assert profile.endswith(f"{PAGE_MODEL_MARK}nemotron-kmw")
    other = type("Other", (Big,), {"page_model": "nemotron-kmw-2"})()
    assert profile_key(profile) == profile_key(extraction_profile(other, ocr=False))
    assert profile_key(profile) != profile_key(extraction_profile(None, ocr=False))


# -- stored ---------------------------------------------------------------------------------------------------
def _page_texts(conn, name: str) -> dict:
    return {r["page"]: dict(r) for r in conn.execute(
        "SELECT p.* FROM page_texts p JOIN documents d ON d.id = p.doc_id WHERE d.rel_path = ?", (name,))}


def test_document_with_only_layer_pages_is_complete(tmp_path):
    conn, root = setup_index(tmp_path, {"Konzept.pdf": three_pages()})
    stage1(conn, root, Big())
    doc = docs(conn)["Konzept.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("ok", 0, 3)
    assert doc["message"] == "Seitentyp ausstehend: 3 Seite(n)"
    assert PAGE_MODEL_MARK in doc["extract_profile"]
    rows = _page_texts(conn, "Konzept.pdf")
    assert [rows[n]["state"] for n in (1, 2, 3)] == ["pending"] * 3
    assert [rows[n]["layer"] for n in (1, 2, 3)] == [1, 1, 1]
    assert all(rows[n]["segments"] for n in (1, 2, 3))  # the lines with their place wait with the page
    assert "Außentreppe" in text_of(conn, "Konzept.pdf")  # searchable at once


def test_scan_pages_keep_the_document_incomplete(tmp_path):
    conn, root = setup_index(tmp_path, {"Scan.pdf": scan_pdf(2, layer=OCR), "Alt.pdf": scan_pdf(1)})
    stage1(conn, root, Big())
    doc = docs(conn)["Scan.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("ok", 1, 2)
    assert doc["message"] == "Bildauswertung ausstehend: 2 Seite(n)"
    assert [r["layer"] for r in _page_texts(conn, "Scan.pdf").values()] == [0, 0]
    old = docs(conn)["Alt.pdf"]  # a scan without any text: nothing to search yet
    assert (old["status"], old["incomplete"], old["vision_pending"]) == ("skipped", 1, 1)


def test_same_content_keeps_types(tmp_path):
    conn, root = setup_index(tmp_path, {"Konzept.pdf": three_pages()})
    stage1(conn, root, Big())
    doc = docs(conn)["Konzept.pdf"]
    seitentypen.store(conn, doc["id"], 2, typ="Text", layer=True, raw="<x_0.1><y_0.1>x<x_0.2><y_0.2><class_Text>")
    conn.execute("UPDATE documents SET incomplete = 1, retry_after = '2000-01-01 00:00:00' WHERE id = ?", (doc["id"],))
    conn.commit()
    stage1(conn, root, Big())  # read again: same content, same profile
    rows = _page_texts(conn, "Konzept.pdf")
    assert [rows[n]["state"] for n in (1, 2, 3)] == ["pending", "done", "pending"]
    assert sorted(seitentypen.known(conn, doc["id"])) == [2]
    assert docs(conn)["Konzept.pdf"]["vision_pending"] == 2
    assert P2 in text_of(conn, "Konzept.pdf")


def test_changed_file_loses_its_types(tmp_path):
    conn, root = setup_index(tmp_path, {"Konzept.pdf": three_pages()})
    stage1(conn, root, Big())
    doc = docs(conn)["Konzept.pdf"]
    seitentypen.store(conn, doc["id"], 2, typ="Text", layer=True)
    conn.commit()
    path = root / "BS-2024-0001 Halle" / "Konzept.pdf"
    path.write_bytes(pdf_bytes([P1 + " Geändert.", "\f", P2]))
    stage1(conn, root, Big())
    assert seitentypen.known(conn, doc["id"]) == {}
    assert docs(conn)["Konzept.pdf"]["vision_pending"] == 2


def test_complete_pdf_without_mark_is_due():
    new = extraction_profile(Big(), ocr=False)
    old = extraction_profile(None, ocr=False)
    now = "2026-11-26 10:00:00"

    def row(ext: str, profile: str = old) -> dict:
        return {"incomplete": 0, "extract_profile": profile, "retry_after": None, "vision_pending": 0, "ext": ext}

    assert reprocess_due(row(".pdf"), new, now) is True
    assert reprocess_due(row(".png"), new, now) is True
    assert reprocess_due(row(".docx"), new, now) is False
    assert reprocess_due(row(".pdf", new), new, now) is False  # read by this reader already
    assert reprocess_due(row(".pdf"), old, now) is False  # without the page model: as before
    # read with the page model by the reader before this one: once more (the model's answers are kept)
    assert reprocess_due(row(".pdf", new.replace(PAGE_MODEL_MARK, ":seitenmodell:")), new, now) is True


def test_files_read_before_the_page_model_are_read_again(tmp_path):
    conn, root = setup_index(tmp_path, {"Konzept.pdf": three_pages(), "Notiz.txt": "Nur Text ohne Seiten.".encode()})
    stage1(conn, root, None)
    assert docs(conn)["Konzept.pdf"]["vision_pending"] == 0
    stats = stage1(conn, root, Big())
    found = docs(conn)
    assert found["Konzept.pdf"]["vision_pending"] == 3
    assert found["Notiz.txt"]["vision_pending"] == 0
    assert stats["reprocessed"] == 1
    assert stage1(conn, root, Big())["reprocessed"] == 0  # once
