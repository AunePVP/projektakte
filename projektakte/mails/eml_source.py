"""Mail source 'folder with .eml files' (MAIL_DIR). Read-only; incremental via size/mtime + hash.

* Each mail is parsed and its attachments are extracted before anything is written; every mail is
  committed on its own, so decisions in the inbox never wait long for the database. A mail that
  cannot be read is logged, counted and tried again on the next run – the import goes on.
* A file moved or renamed inside MAIL_DIR keeps its mail (id, sections, vectors, human decision):
  it is recognised by its content hash, or else by its Message-ID.
* A missing or suddenly empty MAIL_DIR (network drive not mounted yet) and unreadable sub-folders
  delete nothing – the result carries a warning instead.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .model import parse_eml
from .store import delete_mail, prepare_mail, rename_mail, upgrade_mail_texts, write_mail

log = logging.getLogger("projektakte.mails.eml")


def _scan(mail_dir: Path) -> tuple[list[tuple[str, Path]], list[str]]:
    """All .eml files below ``mail_dir`` as (source key, path), plus folders that could not be read."""
    files: list[tuple[str, Path]] = []
    errors: list[str] = []
    for dirpath, dirnames, filenames in os.walk(mail_dir, onerror=lambda e: errors.append(str(e))):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.lower().endswith(".eml") or name.startswith("."):
                continue
            path = Path(dirpath) / name
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

    def match(self, sha256: str, message_id: str | None) -> sqlite3.Row | None:
        """The one vanished mail with the same content, else the one with the same Message-ID."""
        same = self._only("sha256", sha256)
        if not same:
            same = self._only("message_id", message_id)
        return self.rows[same[0]] if len(same) == 1 else None

    def take(self, key: str) -> None:
        self.rows.pop(key, None)


def _set_stamp(conn: sqlite3.Connection, key: str, stamp: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value", (f"eml_stamp:{key}", stamp))


def sync_eml_folder(conn: sqlite3.Connection, mail_dir: Path, *, cache_dir: Path, llm=None,
                    allow_empty: bool = False) -> dict:
    """Import new/changed .eml files, follow moved files, remove mails whose file is gone.

    ``allow_empty`` permits removing all mails when MAIL_DIR is empty (normally a sign of a missing
    mount, so nothing is removed then)."""
    stats = {"new": 0, "changed": 0, "unchanged": 0, "moved": 0, "removed": 0, "errors": 0}
    upgrade_mail_texts(conn)
    mail_dir = Path(mail_dir)
    known = {r["source_key"]: r for r in conn.execute(
        "SELECT m.id, m.source_key, m.sha256, m.message_id, s.value AS stamp FROM mails m "
        "LEFT JOIN meta s ON s.key = 'eml_stamp:' || m.source_key WHERE m.source = 'eml'")}
    if not mail_dir.is_dir():
        if known:
            msg = (f"MAIL_DIR {mail_dir} ist nicht erreichbar – die {len(known)} bekannten Mails bleiben "
                   "unverändert. Ist das Laufwerk eingebunden?")
            log.warning(msg)
            return {**stats, "warning": msg}
        return {**stats, "note": f"MAIL_DIR {mail_dir} existiert nicht"}
    files, scan_errors = _scan(mail_dir)
    if not files and known and not allow_empty:
        msg = (f"MAIL_DIR {mail_dir} enthält keine .eml-Dateien mehr (bisher {len(known)}) – nichts "
               "gelöscht. Ist das Laufwerk eingebunden?")
        log.warning(msg)
        return {**stats, "warning": msg}
    seen = {key for key, _ in files}
    vanished = _Vanished({k: r for k, r in known.items() if k not in seen})
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
            continue
        moved = None
        try:
            rec = parse_eml(path.read_bytes(), key, fallback_date=datetime.fromtimestamp(st.st_mtime, timezone.utc))
            if row is None:
                moved = vanished.match(rec.content_hash, rec.message_id)
                if moved is not None:  # same mail, new place: keep it (short transaction of its own)
                    rename_mail(conn, moved["id"], moved["source_key"], key)
                    conn.execute("DELETE FROM meta WHERE key = ?", (f"eml_stamp:{moved['source_key']}",))
                    conn.commit()
                    vanished.take(moved["source_key"])
            prepared = prepare_mail(conn, rec, cache_dir=cache_dir, llm=llm)  # slow part, no write lock
            _, status = write_mail(conn, prepared)
            _set_stamp(conn, key, stamp)
            conn.commit()
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
    else:
        for key, row in vanished.rows.items():
            delete_mail(conn, row["id"])
            conn.execute("DELETE FROM meta WHERE key = ?", (f"eml_stamp:{key}",))
            stats["removed"] += 1
    conn.commit()
    return stats
