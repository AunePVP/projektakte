"""Index project files: extract → chunk → store (SQLite + FTS5). Incremental via size/mtime and
SHA-256: unchanged files are not read again, changed files are re-processed, deleted files removed.
Incomplete results (scans without vision, failed model calls, errors) are processed again when the
extraction profile changes or after a back-off – and those cut short by a limit of the extraction
worker once that limit is raised. Files are extracted outside the write transaction and committed one
by one, so the web UI never waits long for the database."""
from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import extract_worker
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


# Separates the extraction profile from the limit that cut a result short in the stored profile, e.g.
# "v6:vision:qwen|limit:cpu=120" (see stored_profile).
LIMIT_MARK = "|limit:"


def stored_profile(profile: str, result: Extracted) -> str:
    """The profile stored with a document: the extraction profile, and for a result cut short by a limit
    of the extraction worker (``Extracted.limit``) that limit – only these documents depend on the
    limits, so changing them does not touch any other incomplete document."""
    return f"{profile}{LIMIT_MARK}{result.limit}" if result.limit else profile


def reprocess_due(row, profile: str, now: str, limits: extract_worker.Limits | None = None) -> bool:
    """Should an unchanged file be extracted again?

    Only incomplete results (scan without vision, failed model call, error) are: when the extraction
    profile changed (e.g. LLM_VISION switched on, another model), when their retry time is due, or – for
    a result cut short by a limit of the extraction worker (EXTRACT_CPU_SECONDS, EXTRACT_MEMORY_MB) –
    when ``limits``, those the file would be read under now (None: none, it is read in this process),
    raise that limit."""
    if not row["incomplete"]:
        return False
    base, marked, limit = (row["extract_profile"] or "").partition(LIMIT_MARK)
    if base != profile:
        return True
    if marked and (limits is None or limits.lifts(limit)):
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
    caller should not hold a write transaction meanwhile.

    During an indexing pass with isolation switched on, PDF, Word and mail files are read in the
    extraction worker process with operating-system limits (see extract_worker); it raises
    ExtractionAborted when the app shuts down meanwhile – nothing is to be written for the file then."""
    filename = Path(rel_path).name
    result = extract_worker.extract_file(filename, data, llm, extract)
    chunks = chunk_pages(result.pages) if result.status == "ok" else []
    return PreparedDocument(
        rel_path=rel_path, filename=filename, ext=Path(filename).suffix.lower(), size=len(data),
        sha256=sha256(data), mtime=mtime, doc_date=doc_date or document_date(filename, result.text, mtime),
        pages=max((p.number or 0 for p in result.pages), default=0) or None, result=result,
        chunks=chunks, profile=stored_profile(extraction_profile(llm), result))


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
    """(Re)index one file's content in one call; returns the document id.

    Convenience for one-off callers (scripts, tests). It extracts while the caller may hold a write
    transaction, so the indexing passes use :func:`prepare_document` (no lock) and
    :func:`write_document` (short transaction) instead."""
    doc = prepare_document(rel_path=rel_path, data=data, mtime=mtime, llm=llm, doc_date=doc_date)
    return write_document(conn, doc, path=path, project=project, project_name=project_name, mail_id=mail_id)


# -- derived data that follows the project register ------------------------------------------------
def _delete_documents(conn: sqlite3.Connection, where: str, args: tuple = ()) -> int:
    ids = [r[0] for r in conn.execute(f"SELECT id FROM documents WHERE {where}", args)]
    for doc_id in ids:
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))  # trigger removes FTS rows
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return len(ids)


_PROJECT_FILE_CHUNKS = ("SELECT c.id FROM chunks c JOIN documents d ON d.id = c.doc_id "
                        "WHERE d.project = ? AND d.mail_id IS NULL")


def hide_project_documents(conn: sqlite3.Connection, project: str) -> int:
    """Take the files of a project whose folder is missing out of search and answers, but keep them:
    their full-text rows go (vector search skips files of inactive projects, see vectors.py), while
    documents, sections and vectors stay, so they return unchanged if the folder comes back.
    Returns the number of hidden documents."""
    conn.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({_PROJECT_FILE_CHUNKS})", (project,))
    return conn.execute("SELECT COUNT(*) FROM documents WHERE project = ? AND mail_id IS NULL",
                        (project,)).fetchone()[0]


def restore_project_documents(conn: sqlite3.Connection, project: str, project_name: str | None) -> int:
    """The folder of a hidden project is back: its files return to search (full-text rows written
    again from the stored sections – nothing is extracted or embedded). Returns the number of
    documents."""
    rows = conn.execute(
        "SELECT c.id, c.heading, c.text, d.filename FROM chunks c JOIN documents d ON d.id = c.doc_id "
        "WHERE d.project = ? AND d.mail_id IS NULL", (project,)).fetchall()
    for r in rows:
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (r["id"],))  # never twice
        add_fts(conn, r["id"], fts_title(r["filename"], project, project_name, r["heading"]), r["text"])
    return conn.execute("SELECT COUNT(*) FROM documents WHERE project = ? AND mail_id IS NULL",
                        (project,)).fetchone()[0]


def remove_project_documents(conn: sqlite3.Connection, project: str) -> int:
    """Drop the indexed files of a project whose folder has been missing beyond the grace period
    (derived data – they are indexed again if the folder ever returns). Mails and their attachments
    stay, as do human decisions."""
    return _delete_documents(conn, "mail_id IS NULL AND project = ?", (project,))


def purge_inactive_documents(conn: sqlite3.Connection) -> int:
    """Drop project files that belong to no known project, or to an inactive project without a
    running grace period (left over by older versions, which removed them at once). Files of a
    project whose folder went missing recently stay hidden until the register removes them."""
    return _delete_documents(conn, "mail_id IS NULL AND (project IS NULL OR project NOT IN "
                                   "(SELECT number FROM projects WHERE active = 1 OR missing_since IS NOT NULL))")


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


# -- names that are no valid UTF-8 --------------------------------------------------------------------
# A share mounted without iocharset=utf8 (SMB) delivers names that are no valid UTF-8; Python keeps
# their bytes as lone surrogates (PEP 383). Such names can be read with the real path but never be
# written to the database, a JSON answer or a UTF-8 terminal: they are shown and stored with U+FFFD.
ENCODING_HINT = "Freigabe mit iocharset=utf8 einbinden"
MAX_BAD_EXAMPLES = 8  # file names in a wrong encoding named per pass


def has_bad_encoding(text: str) -> bool:
    """Does a file or folder name hold bytes that are no valid UTF-8 (surrogate escapes)?"""
    return any("\ud800" <= c <= "\udfff" for c in text)


def printable(text: str) -> str:
    """A file or folder name for display and storage: every byte that is no valid UTF-8 as �."""
    if not has_bad_encoding(text):
        return text
    return "".join("\ufffd" if "\ud800" <= c <= "\udfff" else c for c in text)


def real_folder(stored: Path | str) -> Path | None:
    """The real path of a folder whose path is stored with � for bytes that are no valid UTF-8
    (:func:`printable`): every component holding � is looked up among the entries of its parent
    folder whose printable name is that component (two names that differ only in such bytes are
    both followed, the rest of the path decides). None if no folder or more than one fits, or a
    folder on the way cannot be listed. Paths without � are returned as they are."""
    path = Path(stored)
    if "\ufffd" not in str(path):
        return path
    candidates = [Path(path.parts[0])]
    for part in path.parts[1:]:
        found: list[Path] = []
        for folder in candidates:
            if "\ufffd" not in part:
                if (folder / part).is_dir():
                    found.append(folder / part)
                continue
            try:
                with os.scandir(folder) as entries:
                    found += [folder / e.name for e in entries if printable(e.name) == part and e.is_dir()]
            except OSError:
                continue
        if not found:
            return None
        candidates = found
    return candidates[0] if len(candidates) == 1 else None


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


def link_problem(link: Path, boundary: Path) -> str | None:
    """Why a symbolic link is not followed (German, for the log) – target missing, a loop, not
    checkable, or outside ``boundary`` (PROJECTS_DIR, resolved) – or None if its target is inside."""
    try:
        target = link.resolve(strict=True)
    except FileNotFoundError:
        return "Verknüpfung ohne Ziel übersprungen"
    except RuntimeError:  # loop (older Python versions)
        return "Verknüpfung mit Schleife übersprungen"
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return "Verknüpfung mit Schleife übersprungen"
        return f"Verknüpfung nicht prüfbar ({exc.strerror or exc}) – übersprungen"
    if not target.is_relative_to(boundary):
        return "Verknüpfung auf Ziel außerhalb von PROJECTS_DIR übersprungen"
    return None


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
            if f.is_symlink() and (problem := link_problem(f, boundary)):
                warn_once(log, "%s: %s", problem, printable(str(f)))
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
    without holding the write lock, then written and committed on its own.

    The register stores folder names that are no valid UTF-8 with � (:func:`printable`); such a
    project folder is read under its real path (:func:`real_folder`), its documents are stored under
    the printable one. Files whose name (or a folder name inside the project) is no valid UTF-8 cannot
    be stored: they are skipped before they are read – counted in ``skipped`` and ``bad_names``,
    named in ``bad_name_examples`` and warned about once."""
    stored_root = Path(project["path"])
    stats = {"new": 0, "changed": 0, "unchanged": 0, "reprocessed": 0, "moved": 0, "removed": 0,
             "skipped": 0, "errors": 0, "bad_names": 0}
    root = real_folder(stored_root)
    if root is None or not root.is_dir():  # vanished since the register was read, or share not reachable
        log.warning("Projektordner nicht erreichbar, Dokumente bleiben unverändert: %s%s", stored_root,
                    f" – {ENCODING_HINT}" if "\ufffd" in str(stored_root) else "")
        return stats
    boundary = Path(projects_dir).resolve() if projects_dir else root.resolve()
    profile = extraction_profile(llm)
    now = _sqlite_time(_utcnow())
    state = _ProjectState(conn, project["number"])
    seen: set[str] = set()
    walk_errors: list[OSError] = []
    bad: list[str] = []
    if is_inside(root, boundary):
        files = _project_files(root, boundary, walk_errors)
    else:
        warn_once(log, "Projektordner liegt außerhalb von PROJECTS_DIR und wird nicht gelesen: %s",
                  printable(str(root)))
        files = ()
    for f in files:
        if f.suffix.lower() not in SUPPORTED_EXT:
            stats["skipped"] += 1
            continue
        rel_path = f.relative_to(root)
        rel = str(rel_path)
        if has_bad_encoding(rel):  # cannot be stored – never read it (nor extract it again at every pass)
            bad.append(printable(rel))
            stats["skipped"] += 1
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        key = str(stored_root / rel_path)
        seen.add(key)
        row = state.by_path.get(key)
        if row is None and (row := state.relocate(conn, key, rel, stored_root)) is not None:
            conn.commit()
            stats["moved"] += 1
        due = row is not None and reprocess_due(row, profile, now, extract_worker.limits_for(f.name, llm))
        if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime and not due:
            stats["unchanged"] += 1
            continue
        if st.st_size > MAX_FILE_BYTES:
            stats["skipped"] += 1
            continue
        try:
            data = _read_inside(f, boundary)
        except OSError as exc:
            log.warning("Datei nicht lesbar: %s (%s)", key, exc)
            stats["errors"] += 1
            continue
        if row and not due and row["sha256"] == sha256(data):  # touched but not changed
            conn.execute("UPDATE documents SET mtime=?, size=? WHERE id=?", (st.st_mtime, st.st_size, row["id"]))
            conn.commit()
            stats["unchanged"] += 1
            continue
        doc = prepare_document(rel_path=rel, data=data, mtime=st.st_mtime, llm=llm)  # no lock held
        try:
            write_document(conn, doc, path=key, project=project["number"], project_name=project["name"])
        except UnicodeError as exc:  # text that cannot be stored – this file only, the pass goes on
            conn.rollback()
            warn_once(log, "Datei nicht gespeichert, ein Text ist kein gültiges UTF-8: %s (%s)", key, exc)
            stats["errors"] += 1
            continue
        conn.commit()  # one short write transaction per file – the web UI never waits long
        if row is None:
            stats["new"] += 1
        else:
            stats["reprocessed" if due and row["sha256"] == doc.sha256 else "changed"] += 1
    if bad:
        stats["bad_names"] = len(bad)
        stats["bad_name_examples"] = [f"{stored_root.name}/{name}" for name in bad[:MAX_BAD_EXAMPLES]]
        warn_once(log, "Projekt %s: %d Datei(en) nicht gelesen – der Name hat eine falsche Zeichenkodierung "
                       "(z. B. %s) – %s", project["number"], len(bad), ", ".join(bad[:3]), ENCODING_HINT)
    unlisted = []  # folders that could not be listed, as stored (with � where the name is no UTF-8)
    for e in walk_errors:
        if getattr(e, "filename", None):
            try:
                unlisted.append(stored_root / Path(os.fsdecode(e.filename)).relative_to(root))
            except ValueError:
                unlisted.append(Path(printable(os.fsdecode(e.filename))))
    kept = 0
    for key, row in state.by_path.items():
        if key in seen:
            continue
        if any(Path(key).is_relative_to(d) for d in unlisted):
            continue  # folder could not be listed this time – not proof that the file is gone
        if bad and not key.isascii():
            kept += 1  # maybe the same file, listed under a name in a wrong encoding (share mounted
            continue  # without iocharset) – removing it would mean reading it all again after a fix
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (row["id"],))
        conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        stats["removed"] += 1
    if kept:
        warn_once(log, "Projekt %s: %d Dokument(e) mit Umlauten im Namen nicht gefunden, aber behalten – "
                       "vermutlich dieselben Dateien unter falscher Zeichenkodierung (%s)",
                  project["number"], kept, ENCODING_HINT)
    conn.commit()
    return stats


EML_TEXT_META = "eml_text_charset"  # meta: .eml documents with replacement characters were marked once


def mark_garbled_eml_documents(conn: sqlite3.Connection) -> int:
    """Once per database: .eml documents (project files and mail attachments) whose text contains
    replacement characters were extracted by a version that decoded a mail body without charset as
    ASCII. They are marked incomplete with an unknown profile, so they are extracted again – the
    file itself is unchanged and would otherwise be skipped. Returns the number of documents."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (EML_TEXT_META,)).fetchone()
    if row and row[0] == "1":
        return 0
    n = conn.execute(
        "UPDATE documents SET incomplete = 1, extract_profile = '' WHERE ext = '.eml' AND id IN "
        "(SELECT doc_id FROM chunks WHERE doc_id IS NOT NULL AND instr(text, char(65533)) > 0)").rowcount
    conn.execute("INSERT INTO meta(key, value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value = '1'",
                 (EML_TEXT_META,))
    conn.commit()
    if n:
        log.info("%d .eml-Dokument(e) mit Ersatzzeichen werden neu gelesen (Mailtext ohne Zeichensatz)", n)
    return n


def index_all_documents(conn: sqlite3.Connection, llm=None, *, projects_dir: Path | str | None = None) -> dict:
    """Index the files of all active projects (files of unknown projects leave the index first).
    ``projects_dir`` is the boundary for links (default: each project folder itself). A project whose
    files cannot be stored (a name that is no valid UTF-8 the checks missed) is skipped with a warning
    – never the projects after it. ``bad_names`` counts the files skipped for a name in a wrong
    encoding, ``bad_name_examples`` names some of them (project folder/path)."""
    total: dict = {"removed": purge_inactive_documents(conn), "bad_names": 0, "bad_name_examples": []}
    conn.commit()
    mark_garbled_eml_documents(conn)
    projects = [dict(r) for r in conn.execute("SELECT number, name, path FROM projects WHERE active = 1")]
    for p in projects:
        try:
            stats = index_project_documents(conn, p, llm=llm, projects_dir=projects_dir)
        except UnicodeError as exc:
            conn.rollback()
            warn_once(log, "Projekt %s: Dokumente nicht vollständig gelesen – ein Name ist kein gültiges UTF-8 "
                           "(%s) – %s", p["number"], exc, ENCODING_HINT)
            total["errors"] = total.get("errors", 0) + 1
            continue
        examples = stats.pop("bad_name_examples", [])
        room = MAX_BAD_EXAMPLES - len(total["bad_name_examples"])
        total["bad_name_examples"] += examples[:max(0, room)]
        for k, v in stats.items():
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
        if not reprocess_due(row, profile, now, extract_worker.limits_for(Path(row["rel_path"]).name, llm)):
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
