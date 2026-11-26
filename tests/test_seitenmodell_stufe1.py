"""Stage 1 with the page model (VISION_URL): every page of a PDF or image file is listed for the model, also
pages with a real text layer – they keep their text, the model only gives their type. Without the page model
nothing changes."""
from __future__ import annotations

from pathlib import Path

import pytest

from projektakte import documents, seitentypen
from projektakte.demo.writers import pdf_bytes
from projektakte.documents import reprocess_due
from projektakte.extract import EXTRACT_VERSION, extract, extraction_profile, page_model, profile_key

from .test_bildauswertung_stufen import (_docs, _setup, _stage1, _text, digital_with_logo, ocr_layer_scan,
                                         plain_scan, png, tesseract)  # noqa: F401 – tesseract is a fixture
from .test_vektorseiten import outlined_and_blank

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


def test_page_model_is_named_by_the_llm():
    assert page_model(Big()) == "nemotron-kmw"
    assert page_model(None) == ""
    assert page_model(type("Old", (), {"vision": True, "model": "x"})()) == ""


def test_all_pages_pending_with_page_model():
    r = extract(Path("Konzept.pdf"), three_pages(), llm=Big())
    assert r.status == "ok" and r.incomplete is False
    assert r.vision_pages == [1, 2, 3] and r.layer_pages == [1, 2, 3]
    assert "Außentreppe" in r.pages[1].text
    assert r.cad_pages == [] and r.blank_pages == []


def test_without_page_model_unchanged():
    r = extract(Path("Konzept.pdf"), three_pages(), llm=None)
    assert r.vision_pages == [] and r.layer_pages == []
    assert extract(Path("Konzept.pdf"), three_pages(), llm=None, defer_vision=True).vision_pages == []


def test_scan_page_is_not_a_layer_page(tesseract):
    r = extract(Path("Scan.pdf"), plain_scan(2), llm=Big())
    assert r.vision_pages == [1, 2] and r.layer_pages == []
    assert "Rauchabzug Treppenraum erkannt" in r.pages[0].text  # Tesseract's text for now


def test_scan_with_ocr_layer_is_not_a_layer_page():
    r = extract(Path("Protokoll.pdf"), ocr_layer_scan(), llm=Big())
    assert r.vision_pages == [1] and r.layer_pages == []


def test_digital_page_with_logo_is_a_layer_page():
    r = extract(Path("Bericht.pdf"), digital_with_logo(), llm=Big())
    assert r.vision_pages == [1] and r.layer_pages == [1]


def test_image_file_waits_for_the_page_model(tesseract):
    r = extract(Path("Skizze.png"), png(), llm=Big())
    assert r.vision_pages == [1] and r.layer_pages == []
    assert r.pages[0].text == "Seite gescannt Rauchabzug Treppenraum erkannt"


def test_outlined_page_goes_to_the_page_model_and_blank_page_is_noted(tesseract, monkeypatch):
    monkeypatch.delenv("VISION_SKIP_CAD", raising=False)
    r = extract(Path("Auszug.pdf"), outlined_and_blank(), llm=Big())
    assert r.vision_pages == [1] and r.cad_pages == [] and r.blank_pages == [2, 3]


def test_cad_rule_only_without_text_layer(tesseract, monkeypatch):
    monkeypatch.setenv("VISION_SKIP_CAD", "1")
    r = extract(Path("Plan.pdf"), outlined_and_blank(), llm=Big())
    assert r.cad_pages == [1] and r.vision_pages == []
    # a page without any image but with a real text layer always goes to the model
    r = extract(Path("Konzept.pdf"), three_pages(), llm=Big())
    assert r.vision_pages == [1, 2, 3] and r.cad_pages == []


def test_profile_marks_page_model():
    profile = extraction_profile(Big(), ocr=True)
    assert profile == f"v{EXTRACT_VERSION}:seitenmodell:nemotron-kmw+ocr"
    other = type("Other", (Big,), {"page_model": "nemotron-kmw-2"})()
    assert profile_key(profile) == profile_key(extraction_profile(other, ocr=True))
    assert profile_key(profile) != profile_key(extraction_profile(None, ocr=True))


# -- stored ---------------------------------------------------------------------------------------------------
def _page_texts(conn, name: str) -> dict:
    return {r["page"]: dict(r) for r in conn.execute(
        "SELECT p.* FROM page_texts p JOIN documents d ON d.id = p.doc_id WHERE d.rel_path = ?", (name,))}


def test_document_with_only_layer_pages_is_complete(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    doc = _docs(conn)["Konzept.pdf"]
    assert (doc["status"], doc["incomplete"], doc["vision_pending"]) == ("ok", 0, 3)
    assert doc["message"] == "Seitentyp ausstehend: 3 Seite(n)"
    assert ":seitenmodell:nemotron-kmw" in doc["extract_profile"]
    rows = _page_texts(conn, "Konzept.pdf")
    assert [rows[n]["state"] for n in (1, 2, 3)] == ["pending"] * 3
    assert [rows[n]["layer"] for n in (1, 2, 3)] == [1, 1, 1]
    assert "Außentreppe" in _text(conn, "Konzept.pdf")  # searchable at once


def test_scan_pages_keep_the_document_incomplete(tmp_path, tesseract):
    conn, root = _setup(tmp_path, {"Scan.pdf": plain_scan(2)})
    _stage1(conn, root, Big())
    doc = _docs(conn)["Scan.pdf"]
    assert (doc["incomplete"], doc["vision_pending"]) == (1, 2)
    assert doc["message"] == "Bildauswertung ausstehend: 2 Seite(n)"
    assert [r["layer"] for r in _page_texts(conn, "Scan.pdf").values()] == [0, 0]


def test_cad_and_blank_pages_get_their_type_without_the_model(tmp_path, tesseract, monkeypatch):
    monkeypatch.setenv("VISION_SKIP_CAD", "1")
    conn, root = _setup(tmp_path, {"Plan.pdf": outlined_and_blank()})
    _stage1(conn, root, Big())
    doc = _docs(conn)["Plan.pdf"]
    known = seitentypen.known(conn, doc["id"])
    assert (known[1]["typ"], known[1]["source"]) == ("Plan", "cad")
    assert [(known[n]["typ"], known[n]["source"]) for n in (2, 3)] == [("Leer", "render")] * 2
    assert doc["vision_pending"] == 0


def test_same_content_keeps_types(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    doc = _docs(conn)["Konzept.pdf"]
    seitentypen.store(conn, doc["id"], 2, typ="Text", layer=True, raw="<x_0.1>…")
    conn.execute("UPDATE documents SET incomplete = 1, retry_after = '2000-01-01 00:00:00' WHERE id = ?", (doc["id"],))
    conn.commit()
    _stage1(conn, root, Big())  # read again: same content, same profile
    rows = _page_texts(conn, "Konzept.pdf")
    assert [rows[n]["state"] for n in (1, 2, 3)] == ["pending", "done", "pending"]
    assert sorted(seitentypen.known(conn, doc["id"])) == [2]
    assert _docs(conn)["Konzept.pdf"]["vision_pending"] == 2


def test_changed_file_loses_its_types(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages()})
    _stage1(conn, root, Big())
    doc = _docs(conn)["Konzept.pdf"]
    seitentypen.store(conn, doc["id"], 2, typ="Text", layer=True)
    conn.commit()
    path = root / "BS-2024-0001 Halle" / "Konzept.pdf"
    path.write_bytes(pdf_bytes([P1 + " Geändert.", "\f", P2]))
    _stage1(conn, root, Big())
    assert seitentypen.known(conn, doc["id"]) == {}
    assert _docs(conn)["Konzept.pdf"]["vision_pending"] == 2


def test_complete_pdf_without_mark_is_due():
    new = extraction_profile(Big(), ocr=True)
    now = "2026-11-26 10:00:00"

    def row(ext: str, profile: str = "v8:text+ocr") -> dict:
        return {"incomplete": 0, "extract_profile": profile, "retry_after": None, "vision_pending": 0, "ext": ext}

    old = extraction_profile(None, ocr=True)

    assert reprocess_due(row(".pdf"), new, now) is True
    assert reprocess_due(row(".png"), new, now) is True
    assert reprocess_due(row(".docx"), new, now) is False
    assert reprocess_due(row(".pdf", new), new, now) is False  # typed already
    assert reprocess_due(row(".pdf"), old, now) is False  # without the page model: as before


def test_files_read_before_the_page_model_are_read_again(tmp_path):
    conn, root = _setup(tmp_path, {"Konzept.pdf": three_pages(), "Notiz.txt": "Nur Text ohne Seiten.".encode()})
    _stage1(conn, root, None)
    assert _docs(conn)["Konzept.pdf"]["vision_pending"] == 0
    stats = _stage1(conn, root, Big())
    docs = _docs(conn)
    assert docs["Konzept.pdf"]["vision_pending"] == 3
    assert docs["Notiz.txt"]["vision_pending"] == 0
    assert stats["reprocessed"] == 1
    assert _stage1(conn, root, Big())["reprocessed"] == 0  # once
