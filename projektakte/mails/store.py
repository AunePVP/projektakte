"""Store mails (body + attachments) in the index. Attachments are processed like documents."""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from pathlib import Path

from ..chunking import chunk_pages
from ..documents import add_fts, fts_title, store_document
from ..extract import Page
from ..textutil import fold
from .model import MailRecord

log = logging.getLogger("projektakte.mails")


def _safe_name(name: str) -> str:
    name = re.sub(r"[\\/\x00-\x1f]", "_", name).strip(". ") or "anhang"
    return name[:150]


def mail_text(rec_or_row) -> str:
    g = (lambda k: rec_or_row[k]) if isinstance(rec_or_row, sqlite3.Row) else (lambda k: getattr(rec_or_row, k))
    return (f"Betreff: {g('subject')}\nVon: {g('sender_name')} <{g('sender')}>\nDatum: {g('date') or ''}"
            f"\n\n{g('body')}")


def _index_body(conn: sqlite3.Connection, mail_id: int, rec: MailRecord, project: str | None,
                project_name: str | None) -> None:
    conn.execute("DELETE FROM chunks WHERE mail_id = ? AND doc_id IS NULL", (mail_id,))
    for c in chunk_pages([Page(None, mail_text(rec))]):
        cid = conn.execute(
            "INSERT INTO chunks(doc_id, mail_id, project, ord, page, heading, text) VALUES(NULL,?,?,?,?,?,?)",
            (mail_id, project, c.ord, None, rec.subject, c.text)).lastrowid
        title = fts_title("Mail", project, project_name, rec.subject) + " " + fold(rec.sender_name)
        add_fts(conn, cid, title, c.text)


def store_mail(conn: sqlite3.Connection, rec: MailRecord, *, cache_dir: Path, llm=None) -> tuple[int, str]:
    """Insert or update one mail. Returns (mail id, "new" | "changed" | "unchanged")."""
    row = conn.execute("SELECT id, sha256, project FROM mails WHERE source_key = ?",
                       (rec.source_key,)).fetchone()
    if row and row["sha256"] == rec.content_hash:
        conn.execute("UPDATE mails SET deleted = 0 WHERE id = ?", (row["id"],))
        return row["id"], "unchanged"
    preview = re.sub(r"\s+", " ", rec.body)[:240]
    values = (rec.source, rec.message_id, rec.in_reply_to, rec.subject, rec.sender, rec.sender_name,
              rec.recipients, rec.date, rec.body, preview, rec.content_hash, int(bool(rec.attachments)))
    if row:
        mail_id, status, project = row["id"], "changed", row["project"]
        conn.execute(
            "UPDATE mails SET source=?, message_id=?, in_reply_to=?, subject=?, sender=?, sender_name=?, "
            "recipients=?, date=?, body=?, preview=?, sha256=?, has_attachments=?, deleted=0, "
            "indexed_at=datetime('now') WHERE id=?", (*values, mail_id))
    else:
        status, project = "new", None
        mail_id = conn.execute(
            "INSERT INTO mails(source, message_id, in_reply_to, subject, sender, sender_name, recipients, "
            "date, body, preview, sha256, has_attachments, source_key, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))", (*values, rec.source_key)).lastrowid
    project_name = None
    if project:
        r = conn.execute("SELECT name FROM projects WHERE number = ?", (project,)).fetchone()
        project_name = r[0] if r else None
    _index_body(conn, mail_id, rec, project, project_name)

    # attachments: stored in our own cache (for download) and indexed like documents
    old = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM documents WHERE mail_id = ?",
                                                     (mail_id,))}
    keep = set()
    for i, att in enumerate(rec.attachments):
        digest = hashlib.sha256(att.data).hexdigest()
        target = cache_dir / "attachments" / digest[:2] / digest / _safe_name(att.filename)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(att.data)
        key = f"mail:{mail_id}:{i}:{att.filename}"
        keep.add(key)
        doc_id = store_document(conn, path=key, rel_path=att.filename, project=project,
                                project_name=project_name, data=att.data, mtime=None, llm=llm,
                                mail_id=mail_id, doc_date=(rec.date or "")[:10] or None)
        conn.execute("UPDATE documents SET message = COALESCE(message, ?) WHERE id = ? AND status != 'ok'",
                     (f"Anhang von Mail {mail_id}", doc_id))
        conn.execute("UPDATE chunks SET mail_id = ? WHERE doc_id = ?", (mail_id, doc_id))
        conn.execute("UPDATE documents SET cache_path = ? WHERE id = ?", (str(target), doc_id))
    for path, doc_id in old.items():
        if path not in keep:
            conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return mail_id, status


def delete_mail(conn: sqlite3.Connection, mail_id: int) -> None:
    """Remove a mail from the index (its source disappeared). Human decisions are kept separately."""
    conn.execute("DELETE FROM chunks WHERE mail_id = ?", (mail_id,))
    conn.execute("DELETE FROM documents WHERE mail_id = ?", (mail_id,))
    conn.execute("DELETE FROM mails WHERE id = ?", (mail_id,))
