"""Queries for the assignment inbox, shared by the web interface and the JSON API.

The inbox can hold thousands of mails after the first import of a mailbox, so it is always read
page by page; attachment names are loaded with one query per page instead of one per mail.

The web interface pages by key (keyset), not by offset: a page starts at the sort key of a mail
(suggestion first, date, id – see :data:`SortKey`). Decided mails leave the inbox while their page
is still open; with an offset the next page would then skip as many mails as were decided.

A rejected suggestion stays on its page as an actionable card although it now sorts further back
(behind the suggestions) or, in the filter "Mit Vorschlag", left the filter. Its page counts it;
the following pages get its id (:func:`carried`) and do not count it a second time. A page whose
key range it now falls into still shows it (marked as shown before): no page hides an open mail.
"""
from __future__ import annotations

import re
import sqlite3

from .assign import INBOX_STATUSES

PAGE_SIZE = 50  # mails per page in the web interface
MAX_PAGE_IDS = 2 * PAGE_SIZE  # ids of the cards of one page a decision may send along
MAX_API_LIMIT = 500

_INBOX_COLUMNS = (
    "m.id, m.source, m.subject, m.sender, m.sender_name, m.recipients, m.date, m.preview, "
    "m.has_attachments, m.project, m.assign_status, m.assign_method, m.assign_reason, "
    "m.suggested_project, m.suggestion_score, p.name AS suggested_name"
)
# suggestions first (they need only one click), then the newest mails; id keeps pages stable.
# The key columns in that order; every column is sorted descending.
_KEY = "(m.assign_status = 'suggested'), COALESCE(m.date, ''), m.id"
_INBOX_ORDER = "(m.assign_status = 'suggested') DESC, COALESCE(m.date, '') DESC, m.id DESC"
_INBOX_ORDER_REVERSED = "(m.assign_status = 'suggested') ASC, COALESCE(m.date, '') ASC, m.id ASC"
_FROM = "FROM mails m LEFT JOIN projects p ON p.number = m.suggested_project"

SortKey = tuple[int, str, int]  # (1 for a suggestion else 0, date or '', id) – compared descending
_KEY_TEXT = re.compile(r"^([01])~(\d{1,18})~(.{0,64})$", re.S)


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


def sort_key(mail: dict) -> SortKey:
    """Position of a mail in the inbox order (as it was when the mail was read)."""
    return (1 if mail["assign_status"] == "suggested" else 0, mail["date"] or "", int(mail["id"]))


def encode_key(key: SortKey) -> str:
    """Key as a URL parameter: '1~4711~2024-03-23T14:58:00+00:00'."""
    return f"{key[0]}~{key[2]}~{key[1]}"


def decode_key(text: str | None) -> SortKey | None:
    """Key from a URL parameter; None for missing or malformed values."""
    m = _KEY_TEXT.match(text or "")
    return (int(m.group(1)), m.group(3), int(m.group(2))) if m else None


def _where(status: str) -> tuple[str, tuple]:
    statuses = statuses_for(status)
    return f"m.deleted = 0 AND m.assign_status IN ({','.join('?' * len(statuses))})", statuses


def page_rows(conn: sqlite3.Connection, *, status: str = "", limit: int = PAGE_SIZE, offset: int = 0,
              start: SortKey | None = None, before: SortKey | None = None, last: bool = False) -> list[dict]:
    """One page of inbox mails in inbox order (with suggested project name and attachment names).

    ``start``: the page begins at this key (inclusive) – keyset paging; ``before``: the ``limit``
    mails right before this key; ``last``: the last ``limit`` mails; otherwise ``offset`` (API).
    Every open mail of the range is returned – also one an earlier page still shows (:func:`carried`)."""
    where, args = _where(status)
    limit = max(1, limit)
    if before is not None or last:
        if before is not None:
            where += f" AND ({_KEY}) > (?, ?, ?)"
            args = (*args, *before)
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_INBOX_COLUMNS} {_FROM} WHERE {where} ORDER BY {_INBOX_ORDER_REVERSED} LIMIT ?",
            (*args, limit))][::-1]
    else:
        if start is not None:
            where += f" AND ({_KEY}) <= (?, ?, ?)"
            args = (*args, *start)
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_INBOX_COLUMNS} {_FROM} WHERE {where} ORDER BY {_INBOX_ORDER} LIMIT ? OFFSET ?",
            (*args, limit, 0 if start is not None else max(0, offset)))]
    attachment_names(conn, rows)
    return rows


def count_before(conn: sqlite3.Connection, key: SortKey, *, status: str = "") -> int:
    """Number of open mails that come before ``key`` in inbox order."""
    where, args = _where(status)
    return conn.execute(f"SELECT COUNT(*) FROM mails m WHERE {where} AND ({_KEY}) > (?, ?, ?)",
                        (*args, *key)).fetchone()[0]


def count_between(conn: sqlite3.Connection, first: SortKey, last: SortKey, *, status: str = "") -> int:
    """Number of open mails from ``first`` to ``last`` (both inclusive, inbox order) – what is left of
    a page after decisions."""
    where, args = _where(status)
    return conn.execute(
        f"SELECT COUNT(*) FROM mails m WHERE {where} AND ({_KEY}) <= (?, ?, ?) AND ({_KEY}) >= (?, ?, ?)",
        (*args, *first, *last)).fetchone()[0]


def count_ids(conn: sqlite3.Connection, ids: list[int], *, status: str = "") -> tuple[int, int]:
    """Of the mails ``ids`` (the cards of a page): how many are still in the inbox – in any inbox
    status, a rejected suggestion stays actionable on its page – and how many of those match the
    filter ``status``."""
    ids = list(dict.fromkeys(int(i) for i in ids))[:MAX_PAGE_IDS]
    if not ids:
        return 0, 0
    marks = ",".join("?" * len(ids))
    wanted = statuses_for(status)
    left = in_filter = 0
    for (st,) in conn.execute(f"SELECT assign_status FROM mails WHERE deleted = 0 AND id IN ({marks})", ids):
        if st in INBOX_STATUSES:
            left += 1
            in_filter += 1 if st in wanted else 0
    return left, in_filter


def carried(conn: sqlite3.Connection, ids: list[int], *, behind: SortKey, status: str = "",
            inclusive: bool = False) -> list[int]:
    """Of the mails ``ids`` (cards of a page, or carried from earlier pages): those still open that
    count for a page although they sort behind ``behind`` now (or at it, with ``inclusive``) – a
    rejected suggestion moves behind the suggestions – or no longer match the filter ``status``. The
    pages after ``behind`` must not count them again; one that falls into their range is shown there
    once more, marked as shown before."""
    ids = list(dict.fromkeys(int(i) for i in ids))
    if not ids:
        return []
    wanted = statuses_for(status)
    out = {row["id"] for row in conn.execute(
        f"SELECT id, assign_status, date FROM mails WHERE deleted = 0 AND id IN ({','.join('?' * len(ids))})", ids)
        if row["assign_status"] in INBOX_STATUSES and (
            row["assign_status"] not in wanted or sort_key(row) < behind or (inclusive and sort_key(row) == behind))}
    return [i for i in ids if i in out][:MAX_PAGE_IDS]


def encode_ids(mails: list[dict] | list[int]) -> str:
    """Ids of the cards of a page (mails or ids) as a form value: '12,13,40'."""
    return ",".join(str(int(m["id"] if isinstance(m, dict) else m)) for m in mails)


def decode_ids(text: str | None) -> list[int]:
    """Ids from a form value; malformed parts are ignored, at most :data:`MAX_PAGE_IDS`."""
    out: list[int] = []
    for part in (text or "").split(",")[:MAX_PAGE_IDS]:
        part = part.strip()
        if part.isdigit() and len(part) <= 18:
            out.append(int(part))
    return out


def mail_row(conn: sqlite3.Connection, mail_id: int) -> dict | None:
    """A single mail in the shape of an inbox row (also when it has left the inbox)."""
    row = conn.execute(f"SELECT {_INBOX_COLUMNS} {_FROM} WHERE m.id = ? AND m.deleted = 0",
                       (mail_id,)).fetchone()
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
