"""Remove the plMail mails nobody assigned to a project (copies made before only assigned mails were taken).

    python -m projektakte.mails_bereinigen             # only count
    python -m projektakte.mails_bereinigen --loeschen  # remove them with their attachments and search entries

A plMail mail stays when a human assigned it to a project: in plMail (``plmail:projektakte`` with status
"assigned") or in Projektakte (``assign_decisions``: assign/confirm with a project). Everything else of source
``jmap`` is removed from the database (mail, attachments as documents, sections, vectors) and its attachment
files from the cache. plMail itself is never touched; a mail assigned later is fetched then.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys

from . import db
from .config import load_settings
from .mails.store import delete_mail, prune_cache_files

log = logging.getLogger("projektakte.mails_bereinigen")


def _assigned(external: str | None, decided: bool) -> bool:
    try:
        value = json.loads(external) if external else None
    except ValueError:
        value = None
    if isinstance(value, dict) and value.get("status") in ("assigned", "none"):
        return value["status"] == "assigned" and bool(value.get("project"))
    return decided


def unassigned(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """(mail id, source key) of the plMail mails nobody assigned to a project."""
    decided = {r[0] for r in conn.execute("SELECT source_key FROM assign_decisions WHERE action IN "
                                          "('assign', 'confirm') AND COALESCE(project, '') != ''")}
    return [(r[0], r[1]) for r in conn.execute("SELECT id, source_key, external_assignment FROM mails "
                                               "WHERE source = 'jmap'")
            if not _assigned(r[2], r[1] in decided)]


def purge(conn: sqlite3.Connection, cache_dir) -> dict:
    """Remove them; commits. Returns counts."""
    conn.execute("PRAGMA secure_delete = ON")  # deleted content is overwritten, not left in free pages
    todo = unassigned(conn)
    files: list[str] = []
    attachments = 0
    for mail_id, key in todo:
        attachments += conn.execute("SELECT COUNT(*) FROM documents WHERE mail_id = ?", (mail_id,)).fetchone()[0]
        files += delete_mail(conn, mail_id)
        conn.execute("DELETE FROM meta WHERE key = ?", (f"jmap_fp:{key}",))
    conn.commit()
    removed_files = prune_cache_files(conn, cache_dir, files) if files else 0
    return {"mails": len(todo), "attachments": attachments, "files": removed_files}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m projektakte.mails_bereinigen",
                                 description="plMail-Mails ohne Projektzuordnung aus Projektakte entfernen")
    ap.add_argument("--loeschen", action="store_true", help="wirklich löschen (ohne: nur zählen)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM mails WHERE source = 'jmap'").fetchone()[0]
        todo = unassigned(conn)
        print(f"plMail-Mails in Projektakte: {total}, davon ohne Projektzuordnung: {len(todo)}, "
              f"zugeordnet (bleiben): {total - len(todo)}")
        if not args.loeschen:
            print("Nichts gelöscht. Zum Löschen: --loeschen")
            return 0
        stats = purge(conn, settings.cache_dir)
    finally:
        conn.close()
    print(f"Gelöscht: {stats['mails']} Mail(s) mit {stats['attachments']} Anhang/Anhängen, "
          f"{stats['files']} Datei(en) im Zwischenspeicher. In plMail bleibt alles unverändert.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
