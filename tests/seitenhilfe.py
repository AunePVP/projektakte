"""Helpers for the tests of the page model: a small index over one project folder, read as the app reads it."""
from __future__ import annotations

import os

from projektakte import db, documents
from projektakte.documents import index_all_documents
from projektakte.registry import sync_from_folder

# the OCR layer the test scans come with (pdfbau.scan_pdf(layer=OCR)): the text of a scan page until the model read it
OCR = "Seite gescannt Rauchabzug Treppenraum erkannt"


def setup_index(tmp_path, files: dict[str, bytes], mtimes: dict[str, float] | None = None):
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


def stage1(conn, root, llm):
    with documents.vision_later():
        return index_all_documents(conn, llm, projects_dir=root)


def docs(conn) -> dict:
    return {r["rel_path"]: dict(r) for r in conn.execute("SELECT * FROM documents")}


def text_of(conn, name: str) -> str:
    return "\n".join(r[0] for r in conn.execute(
        "SELECT c.text FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE d.rel_path = ? ORDER BY c.ord", (name,)))
