"""Mails plMail pushes to Projektakte (docs/plmail-push.md) – the only way a mail gets in when plMail pushes.

plMail decides what leaves it: when a human assigns a mail to a project, plMail sends the raw mail (.eml)
with the assignment; when the assignment changes it sends only the assignment; when it is taken back, set to
"kein Projektbezug" or the mail is deleted, it sends a deletion. Projektakte has no access to the mailbox.

A pushed mail is written to a spool folder first (DATA_DIR/plmail-eingang) – the request returns at once –
and imported by the indexer (phase "Mails"): attachments are cached and read like those of every mail. The
mail keeps the key ``jmap:<accountId>:<emailId>`` (same as mails read via JMAP before: decisions, links to
plMail and an existing copy stay). Deletions and assignment changes of stored mails are applied at once.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

from .jmap_source import parse_assignment
from .model import parse_eml
from .store import delete_mail, prepare_mail, prune_cache_files, retry_locked, set_external_assignment, write_mail

log = logging.getLogger("projektakte.mails.push")

SPOOL = "plmail-eingang"
MAX_BYTES = 200 * 1024 * 1024  # a whole mail with attachments
_ID = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


class PushError(ValueError):
    """A request plMail must correct (HTTP 4xx); the message is German and shown to plMail."""


def check_id(value: str, what: str) -> str:
    if not _ID.match(value or ""):
        raise PushError(f"{what} ungültig (erlaubt: Buchstaben, Ziffern, . _ -)")
    return value


def key(account_id: str, email_id: str) -> str:
    return f"jmap:{account_id}:{email_id}"


def spool_dir(data_dir: Path) -> Path:
    return Path(data_dir) / SPOOL


def _stem(account_id: str, email_id: str) -> str:
    return f"{account_id}__{email_id}"


def assignment_or_error(value) -> dict:
    """The assignment plMail sends (same form as the JMAP property ``plmail:projektakte``)."""
    parsed = parse_assignment(value)
    if parsed is None:
        raise PushError('Zuordnung fehlt oder ist ungültig: {"status": "assigned", "project": "…"} erwartet')
    return parsed


def spool(data_dir: Path, account_id: str, email_id: str, eml_path: Path, assignment: dict) -> None:
    """Take over a received .eml (already written to ``eml_path`` inside the spool folder) with its assignment."""
    folder = spool_dir(data_dir)
    stem = _stem(account_id, email_id)
    meta = folder / f"{stem}.json.tmp"
    meta.write_text(json.dumps(assignment, ensure_ascii=False), encoding="utf-8")
    os.replace(eml_path, folder / f"{stem}.eml")
    os.replace(meta, folder / f"{stem}.json")


def new_upload(data_dir: Path) -> Path:
    folder = spool_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=folder, suffix=".upload")
    os.close(fd)
    return Path(name)


def _unspool(data_dir: Path, account_id: str, email_id: str) -> bool:
    stem = _stem(account_id, email_id)
    found = False
    for suffix in (".eml", ".json"):
        try:
            (spool_dir(data_dir) / f"{stem}{suffix}").unlink()
            found = True
        except FileNotFoundError:
            pass
    return found


def delete(conn: sqlite3.Connection, data_dir: Path, cache_dir: Path, account_id: str, email_id: str) -> bool:
    """Remove a mail (stored copy, attachments, waiting upload). True if there was one."""
    spooled = _unspool(data_dir, account_id, email_id)

    def apply() -> list[str] | None:
        row = conn.execute("SELECT id FROM mails WHERE source_key = ?", (key(account_id, email_id),)).fetchone()
        files = delete_mail(conn, row[0]) if row else None
        conn.execute("DELETE FROM meta WHERE key = ?", (f"jmap_fp:{key(account_id, email_id)}",))
        conn.commit()
        return files
    files = retry_locked(conn, apply)
    if files:
        prune_cache_files(conn, cache_dir, files)
    return spooled or files is not None


def reassign(conn: sqlite3.Connection, data_dir: Path, cache_dir: Path, account_id: str, email_id: str,
             assignment: dict) -> str:
    """New assignment for a mail sent before: "assigned" updates it, anything else deletes it.
    Returns "geändert", "gelöscht" or "unbekannt" (plMail must send the mail itself then)."""
    if assignment["status"] != "assigned":
        return "gelöscht" if delete(conn, data_dir, cache_dir, account_id, email_id) else "unbekannt"
    meta = spool_dir(data_dir) / f"{_stem(account_id, email_id)}.json"
    waiting = meta.exists()
    if waiting:
        meta.write_text(json.dumps(assignment, ensure_ascii=False), encoding="utf-8")

    def apply() -> bool:
        row = conn.execute("SELECT id FROM mails WHERE source_key = ?", (key(account_id, email_id),)).fetchone()
        if row:
            set_external_assignment(conn, row[0], assignment)
            conn.commit()
        return row is not None
    stored = retry_locked(conn, apply)
    return "geändert" if stored or waiting else "unbekannt"


def present(conn: sqlite3.Connection, data_dir: Path, account_id: str, email_id: str) -> dict:
    row = conn.execute("SELECT external_assignment FROM mails WHERE source_key = ?",
                       (key(account_id, email_id),)).fetchone()
    waiting = (spool_dir(data_dir) / f"{_stem(account_id, email_id)}.eml").exists()
    return {"vorhanden": row is not None or waiting, "wartet": waiting,
            "zuordnung": json.loads(row[0]) if row and row[0] else None}


def import_spool(conn: sqlite3.Connection, data_dir: Path, *, cache_dir: Path, llm=None) -> dict:
    """Import the waiting pushed mails (indexer, phase "Mails"). A mail that fails stays and is tried again."""
    stats = {"new": 0, "changed": 0, "unchanged": 0, "errors": 0}
    folder = spool_dir(data_dir)
    if not folder.is_dir():
        return stats
    for eml in sorted(folder.glob("*.eml")):
        account_id, _, email_id = eml.stem.partition("__")
        meta = eml.with_suffix(".json")
        try:
            seen = eml.stat().st_mtime_ns, meta.stat().st_mtime_ns
            assignment = assignment_or_error(json.loads(meta.read_text(encoding="utf-8")))
            rec = parse_eml(eml.read_bytes(), key(account_id, email_id))
            rec.source, rec.external_assignment = "jmap", assignment
            rec.compute_hash()
            prepared = prepare_mail(conn, rec, cache_dir=cache_dir, llm=llm)

            def write() -> str:
                if not eml.exists():  # deleted by plMail while it was read: do not bring it back
                    return "unchanged"
                _, status = write_mail(conn, prepared)
                conn.commit()
                return status
            status = retry_locked(conn, write)
        except Exception as exc:  # noqa: BLE001 – one bad mail must not stop the others
            conn.rollback()
            log.warning("Von plMail gesendete Mail %s nicht übernommen (wird erneut versucht): %s", eml.name, exc)
            stats["errors"] += 1
            continue
        # the files go only if no newer upload or assignment replaced them meanwhile
        try:
            if (eml.stat().st_mtime_ns, meta.stat().st_mtime_ns) == seen:
                _unspool(data_dir, account_id, email_id)
        except FileNotFoundError:
            pass
        stats[status] += 1
    for stale in folder.glob("*.upload"):  # interrupted uploads older than a day
        try:
            if stale.stat().st_mtime < time.time() - 86400:
                stale.unlink()
        except OSError:
            pass
    return stats

