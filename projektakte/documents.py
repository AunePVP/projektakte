"""Index project files: extract → chunk → store (SQLite + FTS5). Incremental via size/mtime and
SHA-256: unchanged files are not read again, changed files are re-processed, deleted files removed."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from .chunking import chunk_pages
from .extract import SUPPORTED_EXT, extract
from .textutil import find_date, fold

log = logging.getLogger("projektakte.documents")
MAX_FILE_BYTES = 80 * 1024 * 1024
_FILENAME_DATE = re.compile(r"(?<!\d)(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})(?!\d)")
_DATUM_LINE = re.compile(r"Datum\W{0,5}(\d{1,2}\.\d{1,2}\.\d{4})")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def document_date(filename: str, text: str, mtime: float | None) -> str | None:
    """Date of a document: date in the file name > 'Datum: …' > first date in the text > mtime."""
    m = _FILENAME_DATE.search(filename)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    head = text[:3000]
    m = _DATUM_LINE.search(head)
    d = find_date(m.group(1)) if m else find_date(head)
    if d:
        return d.isoformat()
    if mtime:
        return datetime.fromtimestamp(mtime, timezone.utc).date().isoformat()
    return None


def fts_title(filename: str, project: str | None, project_name: str | None, heading: str | None) -> str:
    return fold(" ".join(x for x in (filename, project, project_name, heading) if x))


def add_fts(conn: sqlite3.Connection, chunk_id: int, title: str, text: str) -> None:
    """Full-text row; text is folded (ä→a, ß→ss) like the queries built in search.py."""
    conn.execute("INSERT INTO chunks_fts(rowid, title, body) VALUES(?, ?, ?)", (chunk_id, title, fold(text)))


def store_document(conn: sqlite3.Connection, *, path: str, rel_path: str, project: str | None,
                   project_name: str | None, data: bytes, mtime: float | None, llm=None,
                   mail_id: int | None = None, doc_date: str | None = None) -> int:
    """(Re)index one file's content; returns the document id."""
    filename = Path(rel_path).name
    ext = Path(filename).suffix.lower()
    result = extract(Path(filename), data, llm=llm)
    chunks = chunk_pages(result.pages) if result.status == "ok" else []
    ddate = doc_date or document_date(filename, result.text, mtime)
    pages = max((p.number or 0 for p in result.pages), default=0) or None
    row = conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()
    values = (project, mail_id, rel_path, filename, ext, len(data), mtime, sha256(data), ddate, pages,
              result.status, result.message, result.method)
    if row:
        doc_id = row[0]
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute(
            "UPDATE documents SET project=?, mail_id=?, rel_path=?, filename=?, ext=?, size=?, mtime=?, "
            "sha256=?, doc_date=?, pages=?, status=?, message=?, method=?, indexed_at=datetime('now') "
            "WHERE id=?", (*values, doc_id))
    else:
        doc_id = conn.execute(
            "INSERT INTO documents(project, mail_id, rel_path, filename, ext, size, mtime, sha256, "
            "doc_date, pages, status, message, method, path, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))", (*values, path)).lastrowid
    for c in chunks:
        cid = conn.execute(
            "INSERT INTO chunks(doc_id, mail_id, project, ord, page, heading, text) VALUES(?,?,?,?,?,?,?)",
            (doc_id, mail_id, project, c.ord, c.page, c.heading, c.text)).lastrowid
        add_fts(conn, cid, fts_title(filename, project, project_name, c.heading), c.text)
    return doc_id


def _project_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith((".", "~$")) or name.lower() in ("thumbs.db", "desktop.ini"):
                continue
            yield Path(dirpath) / name


def index_project_documents(conn: sqlite3.Connection, project: dict, llm=None) -> dict:
    root = Path(project["path"])
    stats = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0, "skipped": 0, "errors": 0}
    known = {r["path"]: r for r in conn.execute(
        "SELECT id, path, size, mtime, sha256, project FROM documents WHERE project = ? AND mail_id IS NULL",
        (project["number"],))}
    seen: set[str] = set()
    for f in _project_files(root):
        if f.suffix.lower() not in SUPPORTED_EXT:
            stats["skipped"] += 1
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        key = str(f)
        seen.add(key)
        row = known.get(key)
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
            stats["unchanged"] += 1
            continue
        if st.st_size > MAX_FILE_BYTES:
            stats["skipped"] += 1
            continue
        try:
            data = f.read_bytes()
        except OSError as exc:
            log.warning("Datei nicht lesbar: %s (%s)", f, exc)
            stats["errors"] += 1
            continue
        if row and row["sha256"] == sha256(data):  # touched but not changed
            conn.execute("UPDATE documents SET mtime=?, size=? WHERE id=?", (st.st_mtime, st.st_size, row["id"]))
            stats["unchanged"] += 1
            continue
        store_document(conn, path=key, rel_path=str(f.relative_to(root)), project=project["number"],
                       project_name=project["name"], data=data, mtime=st.st_mtime, llm=llm)
        stats["changed" if row else "new"] += 1
    for key, row in known.items():
        if key not in seen:
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (row["id"],))
            conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
            stats["removed"] += 1
    return stats


def index_all_documents(conn: sqlite3.Connection, llm=None) -> dict:
    total: dict = {}
    projects = [dict(r) for r in conn.execute("SELECT number, name, path FROM projects WHERE active = 1")]
    for p in projects:
        for k, v in index_project_documents(conn, p, llm=llm).items():
            total[k] = total.get(k, 0) + v
        conn.commit()  # keep transactions short; readers see progress
    return total
