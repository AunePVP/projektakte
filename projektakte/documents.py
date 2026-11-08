"""Index project files: extract → chunk → store (SQLite + FTS5). Incremental via size/mtime and
SHA-256: unchanged files are not read again, changed files are re-processed, deleted files removed.
Incomplete results (scans without vision, failed model calls, errors) are processed again when the
extraction profile changes or after a back-off. Files are extracted outside the write transaction
and committed one by one, so the web UI never waits long for the database."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .chunking import Chunk, chunk_pages
from .extract import SUPPORTED_EXT, Extracted, extract, extraction_profile
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


def mail_fts_title(project: str | None, project_name: str | None, subject: str | None,
                   sender_name: str | None) -> str:
    """Title column of a mail body section (same shape as written by mails/store.py)."""
    return fts_title("Mail", project, project_name, subject) + " " + fold(sender_name or "")


# -- one document: extract (no database) → write (short transaction) ------------------------------
RETRY_BASE = timedelta(minutes=15)  # first retry of a failed extraction, doubled per attempt
RETRY_MAX = timedelta(days=7)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_time(t: datetime) -> str:
    """Same format as SQLite's datetime('now'), so the values compare as text."""
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def retry_delay(attempts: int) -> timedelta:
    """Back-off after the n-th failed attempt: 15 min, 30 min, 1 h, … at most 7 days."""
    return min(RETRY_BASE * (2 ** max(0, min(attempts - 1, 20))), RETRY_MAX)


def reprocess_due(row, profile: str, now: str) -> bool:
    """Should an unchanged file be extracted again?

    Only incomplete results (scan without vision, failed model call, error) are: when the extraction
    profile changed (e.g. LLM_VISION switched on, another model) or when their retry time is due."""
    if not row["incomplete"]:
        return False
    if row["extract_profile"] != profile:
        return True
    return bool(row["retry_after"]) and row["retry_after"] <= now


@dataclass
class PreparedDocument:
    """Everything derived from a file's bytes – computed before any write transaction is opened."""
    rel_path: str
    filename: str
    ext: str
    size: int
    sha256: str
    mtime: float | None
    doc_date: str | None
    pages: int | None
    result: Extracted
    chunks: list[Chunk]
    profile: str


def prepare_document(*, rel_path: str, data: bytes, mtime: float | None, llm=None,
                     doc_date: str | None = None) -> PreparedDocument:
    """Extract and chunk one file. Slow (PDF parsing, vision model) and touches no database, so the
    caller should not hold a write transaction meanwhile."""
    filename = Path(rel_path).name
    result = extract(Path(filename), data, llm=llm)
    chunks = chunk_pages(result.pages) if result.status == "ok" else []
    return PreparedDocument(
        rel_path=rel_path, filename=filename, ext=Path(filename).suffix.lower(), size=len(data),
        sha256=sha256(data), mtime=mtime, doc_date=doc_date or document_date(filename, result.text, mtime),
        pages=max((p.number or 0 for p in result.pages), default=0) or None, result=result,
        chunks=chunks, profile=extraction_profile(llm))


def write_document(conn: sqlite3.Connection, doc: PreparedDocument, *, path: str, project: str | None,
                   project_name: str | None, mail_id: int | None = None) -> int:
    """Store a prepared document with its sections and full-text rows; returns the document id.

    Only fast SQL – the caller commits right after, so the write lock is held briefly."""
    r = doc.result
    row = conn.execute("SELECT id, sha256, attempts FROM documents WHERE path = ?", (path,)).fetchone()
    attempts, retry_after = 0, None
    if r.retry:  # count failures of the same content; a new version starts again
        attempts = (row[2] or 0) + 1 if row and row[1] == doc.sha256 else 1
        retry_after = _sqlite_time(_utcnow() + retry_delay(attempts))
    values = (project, mail_id, doc.rel_path, doc.filename, doc.ext, doc.size, doc.mtime, doc.sha256,
              doc.doc_date, doc.pages, r.status, r.message, r.method, doc.profile, int(r.incomplete),
              attempts, retry_after)
    if row:
        doc_id = row[0]
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
        conn.execute(
            "UPDATE documents SET project=?, mail_id=?, rel_path=?, filename=?, ext=?, size=?, mtime=?, "
            "sha256=?, doc_date=?, pages=?, status=?, message=?, method=?, extract_profile=?, incomplete=?, "
            "attempts=?, retry_after=?, indexed_at=datetime('now') WHERE id=?", (*values, doc_id))
    else:
        doc_id = conn.execute(
            "INSERT INTO documents(project, mail_id, rel_path, filename, ext, size, mtime, sha256, "
            "doc_date, pages, status, message, method, extract_profile, incomplete, attempts, retry_after, "
            "path, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (*values, path)).lastrowid
    for c in doc.chunks:
        cid = conn.execute(
            "INSERT INTO chunks(doc_id, mail_id, project, ord, page, heading, text) VALUES(?,?,?,?,?,?,?)",
            (doc_id, mail_id, project, c.ord, c.page, c.heading, c.text)).lastrowid
        add_fts(conn, cid, fts_title(doc.filename, project, project_name, c.heading), c.text)
    return doc_id


def store_document(conn: sqlite3.Connection, *, path: str, rel_path: str, project: str | None,
                   project_name: str | None, data: bytes, mtime: float | None, llm=None,
                   mail_id: int | None = None, doc_date: str | None = None) -> int:
    """(Re)index one file's content; returns the document id.

    Extracts inside the caller's transaction – long-running callers should use
    :func:`prepare_document` before and :func:`write_document` inside their write transaction."""
    doc = prepare_document(rel_path=rel_path, data=data, mtime=mtime, llm=llm, doc_date=doc_date)
    return write_document(conn, doc, path=path, project=project, project_name=project_name, mail_id=mail_id)


# -- derived data that follows the project register ------------------------------------------------
def _delete_documents(conn: sqlite3.Connection, where: str, args: tuple = ()) -> int:
    ids = [r[0] for r in conn.execute(f"SELECT id FROM documents WHERE {where}", args)]
    for doc_id in ids:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))  # trigger removes FTS rows
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return len(ids)


def remove_project_documents(conn: sqlite3.Connection, project: str) -> int:
    """Drop the indexed files of a project whose folder disappeared (derived data – they come back
    when the folder does). Mails and their attachments stay, as do human decisions."""
    return _delete_documents(conn, "mail_id IS NULL AND project = ?", (project,))


def purge_inactive_documents(conn: sqlite3.Connection) -> int:
    """Drop project files whose project is not active (any left over from older versions)."""
    return _delete_documents(conn, "mail_id IS NULL AND (project IS NULL OR project NOT IN "
                                   "(SELECT number FROM projects WHERE active = 1))")


def refresh_fts_titles(conn: sqlite3.Connection, project: str, project_name: str | None) -> int:
    """Rewrite the full-text titles of all sections of a project (files, mails and attachments)
    after its name changed; texts and vectors stay. Returns the number of sections."""
    rows = conn.execute(
        "SELECT c.id, c.heading, c.doc_id, d.filename, m.sender_name FROM chunks c "
        "LEFT JOIN documents d ON d.id = c.doc_id LEFT JOIN mails m ON m.id = c.mail_id "
        "WHERE c.project = ?", (project,)).fetchall()
    for r in rows:
        if r["doc_id"]:
            title = fts_title(r["filename"], project, project_name, r["heading"])
        else:
            title = mail_fts_title(project, project_name, r["heading"], r["sender_name"])
        conn.execute("UPDATE chunks_fts SET title = ? WHERE rowid = ?", (title, r["id"]))
    return len(rows)


# -- project folders --------------------------------------------------------------------------------
_warned: set[tuple] = set()


def warn_once(logger: logging.Logger, message: str, *args) -> None:
    """Log a warning about a lasting condition (e.g. a link to outside) once per process, not on
    every indexing pass."""
    key = (logger.name, message, *map(str, args))
    if key in _warned:
        logger.debug(message, *args)
        return
    _warned.add(key)
    logger.warning(message, *args)


def is_inside(path: Path, boundary: Path) -> bool:
    """Does ``path`` (symlinks resolved) lie below ``boundary`` (already resolved)?"""
    try:
        return path.resolve(strict=True).is_relative_to(boundary)
    except (OSError, RuntimeError):  # broken link, loop, permission
        return False


def _project_files(root: Path, boundary: Path | None = None, errors: list | None = None):
    """Files of a project folder. Linked folders are never entered; linked files only count when
    their target lies inside ``boundary`` (PROJECTS_DIR) – nothing from outside gets indexed.
    Folders that cannot be listed are appended to ``errors``."""
    boundary = boundary or Path(root).resolve()
    onerror = errors.append if errors is not None else None
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith((".", "~$")) or name.lower() in ("thumbs.db", "desktop.ini"):
                continue
            f = Path(dirpath) / name
            if f.is_symlink() and not is_inside(f, boundary):
                warn_once(log, "Verknüpfung auf Ziel außerhalb von PROJECTS_DIR übersprungen: %s", f)
                continue
            yield f


def _read_inside(f: Path, boundary: Path) -> bytes:
    """Read a file only if its real location is inside ``boundary``; the final component is opened
    without following links, so a file swapped for a link meanwhile is refused."""
    real = f.resolve(strict=True)
    if not real.is_relative_to(boundary):
        raise PermissionError(f"Ziel liegt außerhalb von PROJECTS_DIR: {real}")
    fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


class _ProjectState:
    """Documents of one project known to the index, by path and by path inside the project."""

    def __init__(self, conn: sqlite3.Connection, number: str):
        self.by_path = {r["path"]: dict(r) for r in conn.execute(
            "SELECT id, path, rel_path, size, mtime, sha256, incomplete, extract_profile, retry_after "
            "FROM documents WHERE project = ? AND mail_id IS NULL", (number,))}
        self.by_rel = {r["rel_path"]: r for r in self.by_path.values()}

    def relocate(self, conn: sqlite3.Connection, key: str, rel: str, root: Path) -> dict | None:
        """A known file of this project under another folder path (folder renamed or moved, number
        kept): move the document to the new path instead of extracting and embedding it again."""
        old = self.by_rel.get(rel)
        if old is None or old["path"] == key or Path(old["path"]).is_relative_to(root):
            return None
        if conn.execute("SELECT 1 FROM documents WHERE path = ?", (key,)).fetchone():
            return None
        conn.execute("UPDATE documents SET path = ? WHERE id = ?", (key, old["id"]))
        del self.by_path[old["path"]]
        old["path"] = key
        self.by_path[key] = old
        return old


def index_project_documents(conn: sqlite3.Connection, project: dict, llm=None, *,
                            projects_dir: Path | str | None = None) -> dict:
    """Bring the documents of one project folder up to date. Every file is read and extracted
    without holding the write lock, then written and committed on its own."""
    root = Path(project["path"])
    stats = {"new": 0, "changed": 0, "unchanged": 0, "reprocessed": 0, "moved": 0, "removed": 0,
             "skipped": 0, "errors": 0}
    if not root.is_dir():  # vanished since the register was read, or share not reachable: keep all
        log.warning("Projektordner nicht erreichbar, Dokumente bleiben unverändert: %s", root)
        return stats
    boundary = Path(projects_dir).resolve() if projects_dir else root.resolve()
    profile = extraction_profile(llm)
    now = _sqlite_time(_utcnow())
    state = _ProjectState(conn, project["number"])
    seen: set[str] = set()
    walk_errors: list[OSError] = []
    if is_inside(root, boundary):
        files = _project_files(root, boundary, walk_errors)
    else:
        warn_once(log, "Projektordner liegt außerhalb von PROJECTS_DIR und wird nicht gelesen: %s", root)
        files = ()
    for f in files:
        if f.suffix.lower() not in SUPPORTED_EXT:
            stats["skipped"] += 1
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        key, rel = str(f), str(f.relative_to(root))
        seen.add(key)
        row = state.by_path.get(key)
        if row is None and (row := state.relocate(conn, key, rel, root)) is not None:
            conn.commit()
            stats["moved"] += 1
        due = row is not None and reprocess_due(row, profile, now)
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime and not due:
            stats["unchanged"] += 1
            continue
        if st.st_size > MAX_FILE_BYTES:
            stats["skipped"] += 1
            continue
        try:
            data = _read_inside(f, boundary)
        except OSError as exc:
            log.warning("Datei nicht lesbar: %s (%s)", f, exc)
            stats["errors"] += 1
            continue
        if row and not due and row["sha256"] == sha256(data):  # touched but not changed
            conn.execute("UPDATE documents SET mtime=?, size=? WHERE id=?", (st.st_mtime, st.st_size, row["id"]))
            conn.commit()
            stats["unchanged"] += 1
            continue
        doc = prepare_document(rel_path=rel, data=data, mtime=st.st_mtime, llm=llm)  # no lock held
        write_document(conn, doc, path=key, project=project["number"], project_name=project["name"])
        conn.commit()  # one short write transaction per file – the web UI never waits long
        if row is None:
            stats["new"] += 1
        else:
            stats["reprocessed" if due and row["sha256"] == doc.sha256 else "changed"] += 1
    unlisted = [Path(e.filename) for e in walk_errors if getattr(e, "filename", None)]
    for key, row in state.by_path.items():
        if key in seen:
            continue
        if any(Path(key).is_relative_to(d) for d in unlisted):
            continue  # folder could not be listed this time – not proof that the file is gone
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (row["id"],))
        conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        stats["removed"] += 1
    conn.commit()
    return stats


def index_all_documents(conn: sqlite3.Connection, llm=None, *, projects_dir: Path | str | None = None) -> dict:
    """Index the files of all active projects. Files of inactive projects leave the index first.
    ``projects_dir`` is the boundary for links (default: each project folder itself)."""
    total: dict = {"removed": purge_inactive_documents(conn)}
    conn.commit()
    projects = [dict(r) for r in conn.execute("SELECT number, name, path FROM projects WHERE active = 1")]
    for p in projects:
        for k, v in index_project_documents(conn, p, llm=llm, projects_dir=projects_dir).items():
            total[k] = total.get(k, 0) + v
    return total


# -- mail attachments --------------------------------------------------------------------------------
def reprocess_attachments(conn: sqlite3.Connection, llm=None) -> dict:
    """Process incomplete mail attachments again (from the attachment cache) when the extraction
    profile changed or their retry time is due – the mail itself is unchanged, so the mail import
    would not look at them again."""
    profile = extraction_profile(llm)
    now = _sqlite_time(_utcnow())
    stats = {"reprocessed": 0, "errors": 0}
    rows = conn.execute(
        "SELECT d.id, d.path, d.rel_path, d.project, d.mail_id, d.doc_date, d.cache_path, d.incomplete, "
        "d.extract_profile, d.retry_after, d.attempts, p.name AS project_name FROM documents d "
        "LEFT JOIN projects p ON p.number = d.project WHERE d.mail_id IS NOT NULL AND d.incomplete = 1"
    ).fetchall()
    for row in rows:
        if not reprocess_due(row, profile, now):
            continue
        try:
            if not row["cache_path"]:
                raise FileNotFoundError("kein Cache-Pfad")
            data = Path(row["cache_path"]).read_bytes()
        except OSError as exc:  # try again later, not on every pass
            log.warning("Anhang nicht im Cache: %s (%s)", row["rel_path"], exc)
            attempts = (row["attempts"] or 0) + 1
            conn.execute("UPDATE documents SET extract_profile = ?, attempts = ?, retry_after = ? WHERE id = ?",
                         (profile, attempts, _sqlite_time(_utcnow() + retry_delay(attempts)), row["id"]))
            conn.commit()
            stats["errors"] += 1
            continue
        doc = prepare_document(rel_path=row["rel_path"], data=data, mtime=None, llm=llm,
                               doc_date=row["doc_date"])
        write_document(conn, doc, path=row["path"], project=row["project"],
                       project_name=row["project_name"], mail_id=row["mail_id"])
        conn.commit()
        stats["reprocessed"] += 1
    return stats
