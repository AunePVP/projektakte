"""Mail source 'folder with .eml files' (MAIL_DIR). Read-only; incremental via size/mtime + hash.

* Each mail is parsed and its attachments are extracted before anything is written; every mail is
  committed on its own, so decisions in the inbox never wait long for the database. A mail that
  cannot be read is logged, counted and tried again on the next run – the import goes on. A write
  that finds the database locked is repeated with a back-off (store.retry_locked).
* A file moved or renamed inside MAIL_DIR keeps its mail (id, sections, vectors, human decision):
  it is recognised by its content hash, or else by its Message-ID – only a real one, and only when
  subject or date match as well (some devices send the same id with every mail).
* Links are followed only to files inside MAIL_DIR, and the file itself is opened without following
  links: nothing from outside MAIL_DIR gets into the index.
* A missing or suddenly empty MAIL_DIR (network drive not mounted yet) and unreadable sub-folders
  delete nothing – the result carries a warning instead.
* Newsletter headers (List-Unsubscribe, Precedence) of mails imported by older versions, which did
  not keep them, are read once from the files (headers only).
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path

from .model import MailRecord, bulk_marker, parse_eml, valid_message_id
from .store import (delete_mail, prepare_mail, rename_mail, retry_locked, set_bulk_marker, upgrade_attachment_dates,
                    upgrade_mail_texts, write_mail)

log = logging.getLogger("projektakte.mails.eml")

BULK_SCAN_META = "mail_bulk_scan"  # meta: newsletter headers of older imports have been read
BULK_SCAN = "1"
HEADER_BYTES = 256 * 1024  # enough for the header of any mail
_warned: set[tuple[str, str]] = set()


def _warn_once(message: str, path: Path) -> None:
    """A lasting condition (a link to outside) is logged once per process, not on every run."""
    key = (message, str(path))
    if key in _warned:
        log.debug(message, path)
        return
    _warned.add(key)
    log.warning(message, path)


def _link_problem(path: Path, boundary: Path) -> str | None:
    """Why a link inside MAIL_DIR is not followed (German, for the log) – or None if its target is a
    file inside MAIL_DIR."""
    try:
        real = path.resolve(strict=True)
    except FileNotFoundError:
        return "Verknüpfung ohne Ziel übersprungen: %s"
    except (OSError, RuntimeError):  # loop, permission
        return "Verknüpfung nicht auflösbar (Schleife?) übersprungen: %s"
    if not real.is_relative_to(boundary):
        return "Verknüpfung auf Ziel außerhalb von MAIL_DIR übersprungen: %s"
    return None


def _read_inside(path: Path, boundary: Path, limit: int = -1) -> bytes:
    """Read a file only if its real location is inside ``boundary`` (MAIL_DIR, resolved); the final
    component is opened without following links, so a file swapped for a link meanwhile is refused."""
    real = path.resolve(strict=True)
    if not real.is_relative_to(boundary):
        raise PermissionError(f"Ziel liegt außerhalb von MAIL_DIR: {real}")
    fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as fh:
        return fh.read(limit)


def _scan(mail_dir: Path, boundary: Path | None = None) -> tuple[list[tuple[str, Path]], list[str]]:
    """All .eml files below ``mail_dir`` as (source key, path), plus folders that could not be read.
    Linked folders are never entered; linked files only count when their target is inside MAIL_DIR."""
    boundary = boundary or Path(mail_dir).resolve()
    files: list[tuple[str, Path]] = []
    errors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(mail_dir, onerror=lambda e: errors.append(str(e))):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.lower().endswith(".eml") or name.startswith("."):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                problem = _link_problem(path, boundary)
                if problem:
                    _warn_once(problem, path)
                    continue
            files.append(("eml:" + str(path.relative_to(mail_dir)), path))
    return files, errors


class _Vanished:
    """Known mails whose file is gone – candidates for a move/rename within MAIL_DIR."""

    def __init__(self, rows: dict[str, sqlite3.Row]):
        self.rows = dict(rows)
        self._by: dict[tuple[str, str], list[str]] = {}
        for key, r in self.rows.items():
            for field in ("sha256", "message_id"):
                if r[field]:
                    self._by.setdefault((field, r[field]), []).append(key)

    def _only(self, field: str, value: str | None) -> list[str]:
        return [k for k in self._by.get((field, value or ""), []) if k in self.rows]

    def match(self, rec: MailRecord) -> sqlite3.Row | None:
        """The one vanished mail with the same content, else the one with the same (real) Message-ID
        and the same subject or date – a constant id of a scanner says nothing on its own."""
        same = self._only("sha256", rec.content_hash)
        if not same and valid_message_id(rec.message_id):
            same = [k for k in self._only("message_id", rec.message_id)
                    if self.rows[k]["subject"] == rec.subject or (rec.date and self.rows[k]["date"] == rec.date)]
        return self.rows[same[0]] if len(same) == 1 else None

    def take(self, key: str) -> None:
        self.rows.pop(key, None)


def _set_stamp(conn: sqlite3.Connection, key: str, stamp: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value", (f"eml_stamp:{key}", stamp))


def _backfill_bulk_markers(conn: sqlite3.Connection, files: list[tuple[str, Path]], boundary: Path) -> int:
    """Once per database: read the newsletter header of mails imported before such headers were kept
    (headers only). A mail that gets a marker and waits in the inbox is evaluated again."""
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (BULK_SCAN_META,)).fetchone()
    if row and row[0] == BULK_SCAN:
        return 0
    parser = BytesHeaderParser(policy=policy.default)
    found: list[tuple[str, str]] = []
    for key, path in files:
        try:
            marker = bulk_marker(parser.parsebytes(_read_inside(path, boundary, HEADER_BYTES)))
        except (OSError, ValueError, LookupError) as exc:
            log.debug("Kopfzeilen nicht lesbar: %s (%s)", path, exc)
            continue
        if marker:
            found.append((key, marker))

    def write() -> None:
        for key, marker in found:
            set_bulk_marker(conn, key, marker)
            conn.execute("UPDATE mails SET suggest_done = 0 WHERE source_key = ? AND assign_status IN "
                         "('unassigned', 'suggested')", (key,))
        conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value = excluded.value", (BULK_SCAN_META, BULK_SCAN))
        conn.commit()

    retry_locked(conn, write)
    if found:
        log.info("Newsletter-Kopfzeilen bei %d bereits importierten Mails nachgetragen", len(found))
    return len(found)


def sync_eml_folder(conn: sqlite3.Connection, mail_dir: Path, *, cache_dir: Path, llm=None,
                    allow_empty: bool = False) -> dict:
    """Import new/changed .eml files, follow moved files, remove mails whose file is gone.

    ``allow_empty`` permits removing all mails when MAIL_DIR is empty (normally a sign of a missing
    mount, so nothing is removed then)."""
    stats = {"new": 0, "changed": 0, "unchanged": 0, "moved": 0, "removed": 0, "errors": 0}
    retry_locked(conn, lambda: upgrade_mail_texts(conn))
    retry_locked(conn, lambda: upgrade_attachment_dates(conn))
    mail_dir = Path(mail_dir)
    known = {r["source_key"]: r for r in conn.execute(
        "SELECT m.id, m.source_key, m.sha256, m.message_id, m.subject, m.date, s.value AS stamp FROM mails m "
        "LEFT JOIN meta s ON s.key = 'eml_stamp:' || m.source_key WHERE m.source = 'eml'")}
    if not mail_dir.is_dir():
        if known:
            msg = (f"MAIL_DIR {mail_dir} ist nicht erreichbar – die {len(known)} bekannten Mails bleiben "
                   "unverändert. Ist das Laufwerk eingebunden?")
            log.warning(msg)
            return {**stats, "warning": msg}
        return {**stats, "note": f"MAIL_DIR {mail_dir} existiert nicht"}
    boundary = mail_dir.resolve()
    files, scan_errors = _scan(mail_dir, boundary)
    if not files and known and not allow_empty:
        msg = (f"MAIL_DIR {mail_dir} enthält keine .eml-Dateien mehr (bisher {len(known)}) – nichts "
               "gelöscht. Ist das Laufwerk eingebunden?")
        log.warning(msg)
        return {**stats, "warning": msg}
    seen = {key for key, _ in files}
    vanished = _Vanished({k: r for k, r in known.items() if k not in seen})
    untouched: list[tuple[str, Path]] = []  # known and unchanged: candidates for the header backfill
    for key, path in files:
        try:
            st = path.stat()
        except OSError as exc:
            log.warning("Mail nicht lesbar: %s (%s)", path, exc)
            stats["errors"] += 1
            continue
        stamp = f"{st.st_size}:{st.st_mtime}"
        row = known.get(key)
        if row and row["stamp"] == stamp:
            stats["unchanged"] += 1
            untouched.append((key, path))
            continue
        moved = None
        try:
            rec = parse_eml(_read_inside(path, boundary), key,
                            fallback_date=datetime.fromtimestamp(st.st_mtime, timezone.utc))
            if row is None:
                moved = vanished.match(rec)
                if moved is not None:  # same mail, new place: keep it (short transaction of its own)
                    def move(old=moved) -> None:
                        rename_mail(conn, old["id"], old["source_key"], key)
                        conn.execute("DELETE FROM meta WHERE key = ?", (f"eml_stamp:{old['source_key']}",))
                        conn.commit()
                    retry_locked(conn, move)
                    vanished.take(moved["source_key"])
            prepared = prepare_mail(conn, rec, cache_dir=cache_dir, llm=llm)  # slow part, no write lock

            def write() -> str:
                _, result = write_mail(conn, prepared)
                _set_stamp(conn, key, stamp)
                conn.commit()
                return result
            status = retry_locked(conn, write)
        except Exception as exc:  # noqa: BLE001 – a broken mail must not stop the import
            conn.rollback()
            log.warning("Mail nicht lesbar: %s (%s)", path, exc)
            stats["errors"] += 1
            continue
        if moved is not None:
            stats["moved"] += 1
            if status == "changed":
                stats["changed"] += 1
        else:
            stats[status] += 1
    if scan_errors:
        msg = (f"Teile von MAIL_DIR {mail_dir} waren nicht lesbar ({scan_errors[0]}) – keine Mail "
               "gelöscht.")
        log.warning(msg)
        stats["warning"] = msg
    elif vanished.rows:
        def remove() -> int:
            for key, row in vanished.rows.items():
                delete_mail(conn, row["id"])
                conn.execute("DELETE FROM meta WHERE key = ?", (f"eml_stamp:{key}",))
            conn.commit()
            return len(vanished.rows)
        stats["removed"] = retry_locked(conn, remove)
    conn.commit()
    _backfill_bulk_markers(conn, untouched, boundary)
    return stats
