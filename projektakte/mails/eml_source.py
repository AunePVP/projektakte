"""Mail source 'folder with .eml files' (MAIL_DIR). Read-only; incremental via size/mtime + hash."""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .model import parse_eml
from .store import delete_mail, store_mail

log = logging.getLogger("projektakte.mails.eml")


def sync_eml_folder(conn: sqlite3.Connection, mail_dir: Path, *, cache_dir: Path, llm=None) -> dict:
    stats = {"new": 0, "changed": 0, "unchanged": 0, "removed": 0, "errors": 0}
    mail_dir = Path(mail_dir)
    if not mail_dir.is_dir():
        return {**stats, "note": f"MAIL_DIR {mail_dir} existiert nicht"}
    known = {r["source_key"]: r for r in conn.execute(
        "SELECT m.id, m.source_key, s.value AS stamp FROM mails m "
        "LEFT JOIN meta s ON s.key = 'eml_stamp:' || m.source_key WHERE m.source = 'eml'")}
    seen = set()
    for dirpath, dirnames, filenames in os.walk(mail_dir):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if not name.lower().endswith(".eml") or name.startswith("."):
                continue
            path = Path(dirpath) / name
            key = "eml:" + str(path.relative_to(mail_dir))
            seen.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            stamp = f"{st.st_size}:{st.st_mtime}"
            row = known.get(key)
            if row and row["stamp"] == stamp:
                stats["unchanged"] += 1
                continue
            try:
                rec = parse_eml(path.read_bytes(), key,
                                fallback_date=datetime.fromtimestamp(st.st_mtime, timezone.utc))
                _, status = store_mail(conn, rec, cache_dir=cache_dir, llm=llm)
            except Exception as exc:  # noqa: BLE001 – a broken mail must not stop the import
                log.warning("Mail nicht lesbar: %s (%s)", path, exc)
                stats["errors"] += 1
                continue
            conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value = excluded.value", (f"eml_stamp:{key}", stamp))
            stats[status] += 1
            if (stats["new"] + stats["changed"]) % 50 == 0:
                conn.commit()
    for key, row in known.items():
        if key not in seen:
            delete_mail(conn, row["id"])
            conn.execute("DELETE FROM meta WHERE key = ?", (f"eml_stamp:{key}",))
            stats["removed"] += 1
    conn.commit()
    return stats
