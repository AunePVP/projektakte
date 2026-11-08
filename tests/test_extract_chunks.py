import email.message
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from projektakte import db
from projektakte.ai.llm import LLMClient
from projektakte.chunking import chunk_pages
from projektakte.config import load_settings
from projektakte.demo import generate
from projektakte.demo.writers import docx_bytes, pdf_bytes, png_bytes
from projektakte.documents import document_date, index_all_documents
from projektakte.extract import Page, decode_text, extract, extraction_profile
from projektakte.indexer import run_index
from projektakte.main import create_app
from projektakte.registry import scan_projects, sync_projects


def test_pdf_pages_are_kept():
    data = pdf_bytes(["Seite eins mit Text über Rauchableitung.", "\f", "Seite zwei: Außentreppe."])
    r = extract(Path("x.pdf"), data)
    assert r.status == "ok" and [p.number for p in r.pages] == [1, 2]
    assert "Außentreppe" in r.pages[1].text


def test_docx_headings_and_tables():
    data = docx_bytes([("h1", "4 Löschwasser"), ("p", "Bedarf siehe Tabelle."),
                       ("table", [("Löschwasser", "192 m³/h")])])
    text = extract(Path("k.docx"), data).text
    assert "# 4 Löschwasser" in text and "Löschwasser | 192 m³/h" in text


def test_text_encodings_and_eml():
    assert extract(Path("a.txt"), "Grüße".encode("cp1252")).text == "Grüße"
    assert extract(Path("a.md"), "﻿# Titel".encode("utf-8")).text == "# Titel"
    m = email.message.EmailMessage()
    m["Subject"] = "Abnahme"
    m.set_content("<p>Termin <b>morgen</b></p>", subtype="html")
    assert "Termin morgen" in extract(Path("m.eml"), m.as_bytes()).text


def test_unsupported_broken_and_empty():
    assert extract(Path("a.dwg"), b"x").status == "skipped"
    assert extract(Path("a.pdf"), b"%PDF-1.4 kaputt").status == "error"
    assert extract(Path("a.txt"), b"   ").status == "empty"


def test_images_need_vision(fake_ai):
    png = png_bytes()
    assert extract(Path("plan.png"), png).status == "skipped"
    no_vision = LLMClient(f"{fake_ai.base_url}/v1", "m", vision=False)
    assert "LLM_VISION=0" in extract(Path("plan.png"), png, llm=no_vision).message
    vision = LLMClient(f"{fake_ai.base_url}/v1", "m", vision=True)
    r = extract(Path("plan.png"), png, llm=vision)
    assert r.status == "ok" and r.method == "vision" and "Fluchtwegplan" in r.text


def test_chunks_overlap_and_respect_size():
    text = "\n".join(f"Auflage {i} (Thema): " + "Satz mit Inhalt. " * 12 for i in range(1, 11))
    chunks = chunk_pages([Page(2, text)], max_chars=700, overlap_chars=250)
    assert len(chunks) > 3
    assert all(len(c.text) <= 700 for c in chunks)
    assert all(c.page == 2 for c in chunks)
    # consecutive chunks share a unit (overlap)
    assert chunks[0].text.split("\n")[-1] == chunks[1].text.split("\n")[0]


def test_long_paragraph_is_split():
    chunks = chunk_pages([Page(None, "Wort " * 600)], max_chars=500)
    assert len(chunks) >= 6 and all(len(c.text) <= 500 for c in chunks)


def test_headings_start_chunks():
    chunks = chunk_pages([Page(None, "# 1 Allgemeines\nText A.\n# 2 Rettungswege\nText B.")])
    assert [c.heading for c in chunks] == ["1 Allgemeines", "2 Rettungswege"]


def test_document_date_priority():
    assert document_date("2025-02-20 Protokoll.txt", "Datum: 01.01.2020", None) == "2025-02-20"
    assert document_date("x.md", "Stand 03.04.2021 … Datum: 05.06.2022", None) == "2022-06-05"
    assert document_date("x.md", "ohne Datum", 0.0 + 86400 * 365) == "1971-01-01"


@pytest.fixture()
def indexed(tmp_path):
    generate(tmp_path / "demo")
    s = load_settings({"AUTH_DISABLED": "1", "DATA_DIR": str(tmp_path), "DEMO_MODE": "1"})
    run_index(s)
    return s


def test_demo_documents_indexed_with_fts(indexed):
    conn = sqlite3.connect(indexed.db_path)
    statuses = dict(conn.execute(
        "SELECT status, COUNT(*) FROM documents WHERE mail_id IS NULL GROUP BY status").fetchall())
    assert statuses["ok"] == 32 and statuses["skipped"] == 1  # the PNG plan without vision
    hits = conn.execute(
        "SELECT d.filename, c.page FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
        "JOIN documents d ON d.id = c.doc_id WHERE chunks_fts MATCH ? AND c.project = ?",
        ('"aussentreppe"', "BS-2024-0137")).fetchall()
    assert ("Baugenehmigung.pdf", 2) in hits


def test_eval_document_facts_are_extractable(indexed):
    """Every file named as a source in tests/eval.json contains the expected answer."""
    conn = sqlite3.connect(indexed.db_path)
    evalset = json.loads((Path(__file__).parent / "eval.json").read_text(encoding="utf-8"))
    for q in evalset:
        for src in q["sources"]:
            if "file" not in src:
                continue
            project = src.get("project") or q["project"]
            text = " ".join(r[0] for r in conn.execute(
                "SELECT c.text FROM chunks c JOIN documents d ON d.id = c.doc_id "
                "WHERE d.project = ? AND d.rel_path = ?", (project, src["file"])))
            assert text, (q["id"], src)
            if q["answer_contains"] and src is q["sources"][0]:
                assert any(a.lower() in text.lower() for a in q["answer_contains"]), q["id"]


def test_incremental_indexing(indexed):
    stats = run_index(indexed)["documents"]
    assert stats["new"] == 0 and stats["changed"] == 0 and stats["unchanged"] == 33
    root = indexed.projects_dir / "BS-2024-0137 Grundschule Am Lindenhof"
    f = root / "04 Protokolle/2025-02-20 Besprechungsprotokoll Nr. 4.txt"
    f.write_text(f.read_text() + "\nTOP 4 – Neu\nEin Sprinkler wird nicht benötigt.\n")
    (root / "06 Notizen").mkdir()
    (root / "06 Notizen/Telefonnotiz.txt").write_text("Rückruf Feuerwehr wegen Hydrant.")
    (root / "03 Prüfung/Prüfbericht Nr. 1.md").unlink()
    stats = run_index(indexed)["documents"]
    assert (stats["new"], stats["changed"], stats["removed"]) == (1, 1, 1)
    conn = sqlite3.connect(indexed.db_path)
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'sprinkler'").fetchone()[0] == 1
    # FTS rows of removed documents are gone
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == \
        conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    # touching without change does not re-extract
    import os
    os.utime(f, (1, 1))
    assert run_index(indexed)["documents"]["changed"] == 0


def test_vision_fallback_indexes_plan(tmp_path, fake_ai):
    generate(tmp_path / "demo")
    s = load_settings({"AUTH_DISABLED": "1", "DATA_DIR": str(tmp_path), "DEMO_MODE": "1"})
    db.init_db(s.db_path)
    with db.session(s.db_path) as conn:
        sync_projects(conn, scan_projects(s.projects_dir, s.project_number_pattern))
        index_all_documents(conn, llm=LLMClient(f"{fake_ai.base_url}/v1", "m", vision=True))
        row = conn.execute("SELECT status, method FROM documents WHERE ext = '.png'").fetchone()
    assert tuple(row) == ("ok", "vision")


def test_documents_api(indexed):
    client = TestClient(create_app(indexed))
    docs = client.get("/api/projects/BS-2024-0137/documents").json()["documents"]
    assert {d["rel_path"] for d in docs} >= {"01 Genehmigung/Baugenehmigung.pdf", "05 Pläne/Fluchtwegplan EG.png"}
    pdf = next(d for d in docs if d["ext"] == ".pdf")
    assert pdf["pages"] == 3 and pdf["doc_date"] == "2024-03-12"
    detail = client.get(f"/api/documents/{pdf['id']}").json()
    assert any("Außentreppe" in c["text"] and c["page"] == 2 for c in detail["chunks"])
    assert client.get("/api/documents/999999").status_code == 404


# -- BUG-12: Unicode text files from Windows tools ----------------------------------------------------
@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-8-sig", "utf-8",
                                      "cp1252"])
def test_text_files_in_unicode_encodings(encoding):
    text = "Rettungsweg Breite 1,20 m – Prüfung (Maße ≥ 0,9 m)" if encoding != "cp1252" else "Grüße – Maß"
    r = extract(Path("Notiz.txt"), text.encode(encoding))
    assert r.status == "ok" and r.text == text


def test_utf16_detection_does_not_misfire_on_binary_like_text():
    assert decode_text(b"ab") == "ab"
    assert decode_text("Grüße".encode("cp1252")) == "Grüße"
    # mostly zero bytes in both positions: not UTF-16 text, falls through to UTF-8/cp1252
    assert decode_text(b"\x00\x00\x00\x00") == "\x00\x00\x00\x00"


# -- BUG-5 / spec-03: incomplete extraction is marked so it can be redone -----------------------------
def test_pdf_scan_pages_are_marked_incomplete_without_vision():
    data = pdf_bytes(["Seite eins mit Text über Rauchableitung.", "\f", "x", "\f", "Seite drei: Außentreppe."])
    r = extract(Path("a.pdf"), data)
    assert r.status == "ok" and [p.number for p in r.pages] == [1, 3]
    assert r.incomplete and not r.retry and "1 Seite(n) ohne Textebene" in r.message
    scan = extract(Path("scan.pdf"), pdf_bytes(["x"]))
    assert (scan.status, scan.incomplete) == ("skipped", True)
    # with vision, a page without any image is just blank – nothing left to read
    vision = _FakeVision()
    assert not extract(Path("a.pdf"), data, llm=vision).incomplete
    assert extract(Path("scan.pdf"), pdf_bytes(["x"]), llm=vision).status == "empty"


class _Img:
    def __init__(self, name="bild.png"):
        self.name, self.data = name, b"\x89PNG"


class _PdfPage:
    def __init__(self, images):
        self.images = images


class _FakeVision:
    vision = True
    model = "vision-x"

    def __init__(self, answers=()):
        self.answers = list(answers)

    def describe_image(self, data, mime, prompt):
        answer = self.answers.pop(0) if self.answers else "Text"
        if isinstance(answer, Exception):
            raise answer

        class R:
            text = answer
        return R()


def test_failed_vision_pages_are_reported_not_dropped():
    from projektakte.extract import _read_scanned_pages
    pages = [(2, _PdfPage([_Img()])), (3, _PdfPage([_Img("a.jpg")])), (4, _PdfPage([])), (5, _PdfPage([_Img()]))]
    read, unread, failed = _read_scanned_pages(pages, _FakeVision(["Grundriss", RuntimeError("timeout"), ""]), "x.pdf")
    assert [p.number for p in read] == [2] and unread == 2 and failed


def test_extraction_errors_are_retryable_and_images_need_vision():
    err = extract(Path("a.pdf"), b"%PDF-1.4 kaputt")
    assert (err.status, err.incomplete, err.retry) == ("error", True, True)
    img = extract(Path("plan.png"), png_bytes())
    assert (img.status, img.incomplete, img.retry) == ("skipped", True, False)
    down = extract(Path("plan.png"), png_bytes(), llm=_FakeVision([RuntimeError("LLM timeout")]))
    assert (down.status, down.incomplete, down.retry) == ("error", True, True)
    empty = extract(Path("plan.png"), png_bytes(), llm=_FakeVision([" "]))
    assert (empty.status, empty.incomplete) == ("empty", True)
    ok = extract(Path("plan.png"), png_bytes(), llm=_FakeVision(["Fluchtwegplan"]))
    assert (ok.status, ok.incomplete, ok.method) == ("ok", False, "vision")


def test_extraction_profile_depends_on_vision_and_model():
    assert extraction_profile(None) == extraction_profile(LLMClient("http://127.0.0.1:9/v1", "m", vision=False))
    a = extraction_profile(LLMClient("http://127.0.0.1:9/v1", "m", vision=True))
    b = extraction_profile(LLMClient("http://127.0.0.1:9/v1", "n", vision=True))
    assert a != b and a != extraction_profile(None)
