"""Queries for the assignment inbox, shared by the web interface and the JSON API.

The inbox can hold thousands of mails after the first import of a mailbox, so it is always read
page by page; attachment names are loaded with one query per page instead of one per mail.
"""
from __future__ import annotations

import sqlite3

from .assign import INBOX_STATUSES

PAGE_SIZE = 50  # mails per page in the web interface
MAX_API_LIMIT = 500

_INBOX_COLUMNS = (
    "m.id, m.source, m.subject, m.sender, m.sender_name, m.recipients, m.date, m.preview, "
    "m.has_attachments, m.project, m.assign_status, m.assign_method, m.assign_reason, "
    "m.suggested_project, m.suggestion_score, p.name AS suggested_name"
)
# suggestions first (they need only one click), then the newest mails; id keeps pages stable
_INBOX_ORDER = "(m.assign_status = 'suggested') DESC, m.date DESC, m.id DESC"


def statuses_for(status: str | None) -> tuple[str, ...]:
    """Inbox statuses selected by a filter value ('' or unknown = all inbox statuses)."""
    return (status,) if status in INBOX_STATUSES else INBOX_STATUSES


def count_open(conn: sqlite3.Connection) -> int:
    """Number of mails waiting in the inbox (navigation badge)."""
    marks = ",".join("?" * len(INBOX_STATUSES))
    return conn.execute(f"SELECT COUNT(*) FROM mails WHERE deleted = 0 AND assign_status IN ({marks})",
                        INBOX_STATUSES).fetchone()[0]


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    """Open mails per inbox status, plus the total under the key ''."""
    marks = ",".join("?" * len(INBOX_STATUSES))
    counts = dict.fromkeys(INBOX_STATUSES, 0)
    counts.update(dict(conn.execute(
        f"SELECT assign_status, COUNT(*) FROM mails WHERE deleted = 0 AND assign_status IN ({marks}) "
        "GROUP BY assign_status", INBOX_STATUSES).fetchall()))
    counts[""] = sum(counts[s] for s in INBOX_STATUSES)
    return counts


def attachment_names(conn: sqlite3.Connection, mails: list[dict]) -> None:
    """Add ``attachments`` (file names in storage order) to every mail dict – one query in total."""
    ids = [m["id"] for m in mails]
    by_mail: dict[int, list[str]] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for mail_id, filename in conn.execute(
                f"SELECT mail_id, filename FROM documents WHERE mail_id IN ({marks}) ORDER BY id", ids):
            by_mail.setdefault(mail_id, []).append(filename)
    for m in mails:
        m["attachments"] = by_mail.get(m["id"], [])


def page_rows(conn: sqlite3.Connection, *, status: str = "", limit: int = PAGE_SIZE,
              offset: int = 0) -> list[dict]:
    """One page of inbox mails (with suggested project name and attachment names)."""
    statuses = statuses_for(status)
    marks = ",".join("?" * len(statuses))
    rows = [dict(r) for r in conn.execute(
        f"SELECT {_INBOX_COLUMNS} FROM mails m LEFT JOIN projects p ON p.number = m.suggested_project "
        f"WHERE m.deleted = 0 AND m.assign_status IN ({marks}) ORDER BY {_INBOX_ORDER} LIMIT ? OFFSET ?",
        (*statuses, max(1, limit), max(0, offset)))]
    attachment_names(conn, rows)
    return rows


def mail_row(conn: sqlite3.Connection, mail_id: int) -> dict | None:
    """A single mail in the shape of an inbox row (also when it has left the inbox)."""
    row = conn.execute(f"SELECT {_INBOX_COLUMNS} FROM mails m LEFT JOIN projects p ON "
                       "p.number = m.suggested_project WHERE m.id = ? AND m.deleted = 0", (mail_id,)).fetchone()
    if not row:
        return None
    mail = dict(row)
    attachment_names(conn, [mail])
    return mail


def is_db_busy(exc: BaseException) -> bool:
    """SQLite gave up waiting for a write lock ("database is locked") – a temporary condition, e.g.
    while the indexer writes; the user can simply try again."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    text = str(exc).lower()
    return "locked" in text or "busy" in text
