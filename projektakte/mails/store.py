"""Store mails (body + attachments) in the index. Attachments are processed like documents.

Storing one mail has two steps:

1. :func:`prepare_mail` does the slow work – copying attachments into our cache and extracting them
   (PDF parsing, possibly the vision model) – without writing to the database.
2. :func:`write_mail` writes the mail, its sections and its attachments inside one SAVEPOINT: a
   failure leaves no half-stored mail behind, so the mail is simply tried again on the next run.

Callers commit right after each mail, so the write lock is only held for a few fast SQL statements
and decisions in the inbox never wait for an extraction or a download. The write transaction takes
the lock up front (``BEGIN IMMEDIATE``): a deferred transaction that read first could not be
upgraded once another connection had committed meanwhile (SQLITE_BUSY_SNAPSHOT – "database is
locked" at once, without waiting). :func:`retry_locked` repeats a write that still finds the
database locked, with a back-off, instead of counting the mail as failed.

An attachment the source could not deliver (plMail answered with an error) is stored as a document
without content – status ``pending`` ("Anhang ausstehend") while it is loaded again later, ``error``
when there is no further attempt – so the mail itself is never held back and nothing disappears
silently.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..chunking import chunk_pages
from ..documents import PreparedDocument, add_fts, fts_title, prepare_document, write_document
from ..extract import Extracted, Page, extraction_profile
from ..textutil import fold
from .model import Attachment, MailRecord, de_datetime, local_day

log = logging.getLogger("projektakte.mails")

MAX_NAME_BYTES = 180  # file systems allow 255 bytes per name; leave room for temporary names
MAX_EXT_BYTES = 16
BULK_META = "mail_bulk:"  # meta key prefix: newsletter header of a mail (by source key)
HEADER_FORMAT_META = "mail_header_format"
HEADER_FORMAT = "de-local-1"  # "Datum: 26.02.2025 16:58" (local time) in the section text
ATTACHMENT_DATES_META = "mail_attachment_dates"
ATTACHMENT_DATES = "local-1"  # doc_date of attachments = local day of the mail (older: UTC day)
PENDING = "pending"  # document status of an attachment that is loaded again later
LOCK_RETRIES = 4  # attempts of a write that finds the database locked (after the busy timeout)
LOCK_BACKOFF = 0.5  # seconds before the second attempt, doubled each time


# -- names and cache ---------------------------------------------------------------------------------
def _cut_utf8(text: str, max_bytes: int) -> str:
    """The longest prefix of ``text`` that fits into ``max_bytes`` UTF-8 bytes (no broken character)."""
    return text.encode("utf-8")[:max(0, max_bytes)].decode("utf-8", "ignore")


def safe_filename(name: str, max_bytes: int = MAX_NAME_BYTES) -> str:
    """File name for the attachment cache: no path separators or control characters, at most
    ``max_bytes`` bytes in UTF-8 (file systems count bytes, not characters), extension kept."""
    name = unicodedata.normalize("NFC", name or "")
    name = re.sub(r"[\\/\x00-\x1f\x7f]", "_", name).strip(". ") or "anhang"
    stem, ext = os.path.splitext(name)
    if not stem or len(ext.encode("utf-8")) > MAX_EXT_BYTES or not ext[1:].isalnum():
        stem, ext = name, ""
    stem = _cut_utf8(stem, max_bytes - len(ext.encode("utf-8"))).rstrip(". ") or "anhang"
    return stem + ext


def cache_attachment(cache_dir: Path, att: Attachment) -> Path:
    """Copy an attachment into the cache (content-addressed, written atomically) and return its path."""
    digest = hashlib.sha256(att.data).hexdigest()
    target = Path(cache_dir) / "attachments" / digest[:2] / digest / safe_filename(att.filename)
    try:
        if target.stat().st_size == len(att.data):
            return target
    except OSError:
        pass  # not cached yet (or unreadable) – write it
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(att.data)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


# -- section text ------------------------------------------------------------------------------------
def mail_text(rec_or_row) -> str:
    """Text of a mail as indexed: a short header (date in local time, as on the mail page) + body."""
    g = (lambda k: rec_or_row[k]) if isinstance(rec_or_row, sqlite3.Row) else (lambda k: getattr(rec_or_row, k))
    return (f"Betreff: {g('subject')}\nVon: {g('sender_name')} <{g('sender')}>\nDatum: {de_datetime(g('date'))}"
            f"\n\n{g('body')}")


def _body_title(project: str | None, project_name: str | None, subject: str, sender_name: str) -> str:
    return fts_title("Mail", project, project_name, subject) + " " + fold(sender_name or "")


def _index_body(conn: sqlite3.Connection, mail_id: int, rec: MailRecord, project: str | None,
                project_name: str | None) -> None:
    chunks = chunk_pages([Page(None, mail_text(rec))])
    old = conn.execute("SELECT ord, text FROM chunks WHERE mail_id = ? AND doc_id IS NULL ORDER BY ord, id",
                       (mail_id,)).fetchall()
    if [(r[0], r[1]) for r in old] == [(c.ord, c.text) for c in chunks]:
        return  # same text (e.g. only an attachment arrived later): sections and vectors stay
    conn.execute("DELETE FROM chunks WHERE mail_id = ? AND doc_id IS NULL", (mail_id,))
    for c in chunks:
        cid = conn.execute(
            "INSERT INTO chunks(doc_id, mail_id, project, ord, page, heading, text) VALUES(NULL,?,?,?,?,?,?)",
            (mail_id, project, c.ord, None, rec.subject, c.text)).lastrowid
        add_fts(conn, cid, _body_title(project, project_name, rec.subject, rec.sender_name), c.text)


_ISO_HEADER_DATE = re.compile(r"(?m)^(Von: [^\n]*\n)Datum: (\d{4}-\d{2}-\d{2}T[^\s]+)$")


def upgrade_mail_texts(conn: sqlite3.Connection) -> int:
    """Once per database: rewrite the header date of mail sections indexed by older versions
    (ISO/UTC "2025-02-26T15:58:00+00:00") to the local German form ("26.02.2025 16:58"). The text
    is changed in place – vectors stay, nothing has to be embedded again. Returns the number of
    changed sections."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (HEADER_FORMAT_META,)).fetchone()
    if row and row[0] == HEADER_FORMAT:
        return 0
    changed = 0
    rows = conn.execute("SELECT id, text FROM chunks WHERE doc_id IS NULL AND mail_id IS NOT NULL "
                        "AND text LIKE '%Datum: ____-__-__T%'").fetchall()
    for cid, text in rows:
        new = _ISO_HEADER_DATE.sub(lambda m: m.group(1) + "Datum: " + (de_datetime(m.group(2)) or m.group(2)),
                                   text)
        if new != text:
            conn.execute("UPDATE chunks SET text = ? WHERE id = ?", (new, cid))
            conn.execute("UPDATE chunks_fts SET body = ? WHERE rowid = ?", (fold(new), cid))
            changed += 1
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (HEADER_FORMAT_META, HEADER_FORMAT))
    conn.commit()
    if changed:
        log.info("Mailkopf in %d Abschnitten auf Ortszeit umgestellt", changed)
    return changed


def upgrade_attachment_dates(conn: sqlite3.Connection) -> int:
    """Once per database: older versions dated mail attachments with the UTC day of the mail (a mail
    at 00:30 in Berlin got the day before). Such dates are set to the local day, like newly stored
    attachments. Returns the number of changed documents."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (ATTACHMENT_DATES_META,)).fetchone()
    if row and row[0] == ATTACHMENT_DATES:
        return 0
    changed = 0
    rows = conn.execute("SELECT d.id, d.doc_date, m.date FROM documents d JOIN mails m ON m.id = d.mail_id "
                        "WHERE m.date IS NOT NULL").fetchall()
    for doc_id, doc_date, date in rows:
        day = local_day(date)
        # only dates written by the older version (= UTC day); anything else is left alone
        if day and doc_date == date[:10] and doc_date != day:
            conn.execute("UPDATE documents SET doc_date = ? WHERE id = ?", (day, doc_id))
            changed += 1
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (ATTACHMENT_DATES_META, ATTACHMENT_DATES))
    conn.commit()
    if changed:
        log.info("Datum von %d Mail-Anhängen auf den Tag in Ortszeit umgestellt", changed)
    return changed


# -- transactions ------------------------------------------------------------------------------------
@contextmanager
def savepoint(conn: sqlite3.Connection, name: str = "store_mail") -> Iterator[None]:
    """All writes inside the block, or none of them. Opens a write transaction if none is open (the
    caller commits, as before); nested use inside a running transaction is fine.

    The transaction is opened with ``BEGIN IMMEDIATE``: the write lock is taken (waiting up to the
    busy timeout) before anything is read, so a commit of another connection between our first read
    and our first write cannot make the write fail at once (SQLITE_BUSY_SNAPSHOT)."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO {name}")
        conn.execute(f"RELEASE {name}")
        raise
    conn.execute(f"RELEASE {name}")


def is_locked(exc: BaseException) -> bool:
    """Is this the database being locked/busy (another connection writes), not a real error?"""
    return isinstance(exc, sqlite3.OperationalError) and any(w in str(exc).lower() for w in ("locked", "busy"))


def retry_locked(conn: sqlite3.Connection, write, *, attempts: int = LOCK_RETRIES, backoff: float = LOCK_BACKOFF,
                 on_retry=None):
    """Run ``write()`` – a short write transaction that commits – and repeat it with a back-off
    (0.5 s, 1 s, 2 s …) while the database stays locked beyond the busy timeout. Other errors and
    the last attempt's error are raised; the transaction is rolled back before each new attempt and
    ``on_retry()`` is called then (to drop in-memory state that was rolled back)."""
    for attempt in range(attempts):
        try:
            return write()
        except sqlite3.OperationalError as exc:
            if not is_locked(exc) or attempt == attempts - 1:
                raise
            conn.rollback()
            if on_retry is not None:
                on_retry()
            delay = backoff * 2 ** attempt
            log.info("Datenbank belegt (%s) – neuer Versuch in %.1f s", exc, delay)
            time.sleep(delay)
    return None  # not reached


# -- one mail ----------------------------------------------------------------------------------------
@dataclass
class PreparedMail:
    """A mail ready to be written: attachments are cached and extracted already."""
    rec: MailRecord
    mail_id: int | None  # existing row (same source key)
    unchanged: bool
    attachments: list[tuple[Attachment, Path, PreparedDocument]] = field(default_factory=list)


def prepare_mail(conn: sqlite3.Connection, rec: MailRecord, *, cache_dir: Path, llm=None) -> PreparedMail:
    """Everything slow, before any write: read-only check whether the mail changed, attachment cache,
    attachment extraction. Raises on errors (nothing is written then)."""
    row = conn.execute("SELECT id, sha256 FROM mails WHERE source_key = ?", (rec.source_key,)).fetchone()
    if row and row["sha256"] == rec.content_hash:
        return PreparedMail(rec, row["id"], True)
    doc_date = local_day(rec.date)
    prepared = []
    for att in rec.attachments:
        if att.unavailable:  # not delivered by the source: a document without content, no cache file
            prepared.append((att, None, _unavailable_document(att, doc_date)))
            continue
        target = cache_attachment(cache_dir, att)
        doc = prepare_document(rel_path=att.filename, data=att.data, mtime=None, llm=llm, doc_date=doc_date)
        prepared.append((att, target, doc))
    return PreparedMail(rec, row["id"] if row else None, False, prepared)


def _unavailable_document(att: Attachment, doc_date: str | None) -> PreparedDocument:
    """Placeholder for an attachment the source did not deliver: status ``pending`` while it is loaded
    again later, else ``error``; the reason is the document's message. Not processed again by the
    attachment re-extraction (there is nothing in the cache) – the mail source reloads it."""
    name = Path(att.filename).name
    return PreparedDocument(rel_path=att.filename, filename=name, ext=Path(name).suffix.lower(), size=att.size or 0,
                            sha256="", mtime=None, doc_date=doc_date, pages=None, chunks=[],
                            result=Extracted(status=PENDING if att.state != "gone" else "error",
                                             message=att.unavailable),
                            profile=extraction_profile(None))


def write_mail(conn: sqlite3.Connection, prepared: PreparedMail) -> tuple[int, str]:
    """Write a prepared mail atomically (SAVEPOINT). Returns (mail id, "new" | "changed" | "unchanged").
    Only fast SQL – commit right after."""
    rec = prepared.rec
    with savepoint(conn):
        row = conn.execute("SELECT id, sha256, project FROM mails WHERE source_key = ?",
                           (rec.source_key,)).fetchone()
        if row and row["sha256"] == rec.content_hash:
            conn.execute("UPDATE mails SET deleted = 0 WHERE id = ?", (row["id"],))
            set_external_assignment(conn, row["id"], rec.external_assignment)
            set_bulk_marker(conn, rec.source_key, rec.bulk)
            return row["id"], "unchanged"
        if prepared.unchanged:  # changed between prepare and write – prepare again next run
            raise RuntimeError(f"Mail {rec.source_key} wurde während der Verarbeitung geändert")
        mail_id, status, project = _write_row(conn, rec, row)
        project_name = None
        if project:
            r = conn.execute("SELECT name FROM projects WHERE number = ?", (project,)).fetchone()
            project_name = r[0] if r else None
        _index_body(conn, mail_id, rec, project, project_name)
        set_external_assignment(conn, mail_id, rec.external_assignment)
        set_bulk_marker(conn, rec.source_key, rec.bulk)
        _write_attachments(conn, mail_id, prepared.attachments, project, project_name)
    return mail_id, status


def _write_row(conn: sqlite3.Connection, rec: MailRecord, row) -> tuple[int, str, str | None]:
    preview = re.sub(r"\s+", " ", rec.body)[:240]
    values = (rec.source, rec.message_id, rec.in_reply_to, rec.subject, rec.sender, rec.sender_name,
              rec.recipients, rec.date, rec.body, preview, rec.content_hash, int(bool(rec.attachments)))
    if row:
        conn.execute(
            "UPDATE mails SET source=?, message_id=?, in_reply_to=?, subject=?, sender=?, sender_name=?, "
            "recipients=?, date=?, body=?, preview=?, sha256=?, has_attachments=?, deleted=0, "
            "assign_status='pending', suggest_done=0, indexed_at=datetime('now') WHERE id=?",
            (*values, row["id"]))
        return row["id"], "changed", row["project"]
    mail_id = conn.execute(
        "INSERT INTO mails(source, message_id, in_reply_to, subject, sender, sender_name, recipients, "
        "date, body, preview, sha256, has_attachments, source_key, indexed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))", (*values, rec.source_key)).lastrowid
    return mail_id, "new", None


def _write_attachments(conn: sqlite3.Connection, mail_id: int, attachments, project: str | None,
                       project_name: str | None) -> None:
    """Attachments are stored in our own cache (for download) and indexed like documents."""
    old = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM documents WHERE mail_id = ?",
                                                     (mail_id,))}
    keep = set()
    for i, (att, target, doc) in enumerate(attachments):
        key = f"mail:{mail_id}:{i}:{att.filename}"
        keep.add(key)
        doc_id = write_document(conn, doc, path=key, project=project, project_name=project_name,
                                mail_id=mail_id)
        conn.execute("UPDATE documents SET message = COALESCE(message, ?) WHERE id = ? AND status != 'ok'",
                     (f"Anhang von Mail {mail_id}", doc_id))
        # no file for an attachment that is still missing ("" – never NULL, links expect a path)
        conn.execute("UPDATE documents SET cache_path = ? WHERE id = ?", (str(target) if target else "", doc_id))
    for path, doc_id in old.items():
        if path not in keep:
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


def store_mail(conn: sqlite3.Connection, rec: MailRecord, *, cache_dir: Path, llm=None) -> tuple[int, str]:
    """Insert or update one mail atomically. Returns (mail id, "new" | "changed" | "unchanged").

    Callers that run long should commit after each mail (the write lock is then held only briefly);
    on an exception nothing of this mail is stored."""
    return write_mail(conn, prepare_mail(conn, rec, cache_dir=cache_dir, llm=llm))


# -- small per-mail facts ----------------------------------------------------------------------------
def set_external_assignment(conn: sqlite3.Connection, mail_id: int, assignment: dict | None) -> bool:
    """Store the assignment made in plMail; a change sends the mail through assignment again."""
    value = json.dumps(assignment, sort_keys=True, ensure_ascii=False) if assignment else None
    old = conn.execute("SELECT external_assignment FROM mails WHERE id = ?", (mail_id,)).fetchone()
    if old is not None and old[0] == value:
        return False
    conn.execute("UPDATE mails SET external_assignment = ?, assign_status = 'pending', suggest_done = 0 "
                 "WHERE id = ?", (value, mail_id))
    return True


def set_bulk_marker(conn: sqlite3.Connection, source_key: str, marker: str | None) -> None:
    """Remember that a mail is a newsletter/mass mailing (header name), or forget it."""
    if marker:
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", (BULK_META + source_key, marker))
    else:
        conn.execute("DELETE FROM meta WHERE key = ?", (BULK_META + source_key,))


def bulk_markers(conn: sqlite3.Connection) -> dict[str, str]:
    """source key → newsletter header, for all mails that have one."""
    n = len(BULK_META)
    return {k[n:]: v for k, v in conn.execute("SELECT key, value FROM meta WHERE key LIKE 'mail\\_bulk:%' "
                                              "ESCAPE '\\'")}


def rename_mail(conn: sqlite3.Connection, mail_id: int, old_key: str, new_key: str) -> None:
    """A mail's source moved (e.g. .eml file moved into another folder): keep the mail – its id,
    sections, vectors and human decision – under the new source key."""
    conn.execute("UPDATE mails SET source_key = ? WHERE id = ?", (new_key, mail_id))
    conn.execute("UPDATE assign_decisions SET source_key = ? WHERE source_key = ? AND NOT EXISTS "
                 "(SELECT 1 FROM assign_decisions WHERE source_key = ?)", (new_key, old_key, new_key))
    conn.execute("UPDATE meta SET key = ? WHERE key = ? AND NOT EXISTS (SELECT 1 FROM meta WHERE key = ?)",
                 (BULK_META + new_key, BULK_META + old_key, BULK_META + new_key))


def delete_mail(conn: sqlite3.Connection, mail_id: int) -> None:
    """Remove a mail from the index (its source disappeared). Human decisions are kept separately."""
    row = conn.execute("SELECT source_key FROM mails WHERE id = ?", (mail_id,)).fetchone()
    conn.execute("DELETE FROM chunks WHERE mail_id = ?", (mail_id,))
    conn.execute("DELETE FROM documents WHERE mail_id = ?", (mail_id,))
    conn.execute("DELETE FROM mails WHERE id = ?", (mail_id,))
    if row:
        conn.execute("DELETE FROM meta WHERE key = ?", (BULK_META + row[0],))
