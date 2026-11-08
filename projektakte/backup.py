"""``python -m projektakte.backup ZIEL`` – consistent backup of the database while the app runs.

Almost everything in DATA_DIR can be rebuilt from the sources by re-indexing – except the human
decisions of the assignment inbox (table ``assign_decisions``). The backup therefore writes

* ``projektakte-<Zeitstempel>.sqlite3`` – a complete, consistent copy made with SQLite's online
  backup API (includes transactions still in the WAL file; copying the file alone may lose them),
  checked with ``PRAGMA integrity_check``;
* ``zuordnungen-<Zeitstempel>.json`` – the human decisions as readable JSON (a second, independent
  safety net that survives schema changes).

ZIEL is a directory (created if necessary). Restore: stop the app, copy the ``.sqlite3`` file to
``DATA_DIR/projektakte.sqlite3``, delete ``projektakte.sqlite3-wal``/``-shm`` there, start the app.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import load_settings


class BackupError(RuntimeError):
    """German message for the operator."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _open_source(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise BackupError(f"Keine Datenbank gefunden: {db_path} (DATA_DIR richtig gesetzt?)")
    # mode=rw: never creates a database at a wrong path; read-write so that a WAL left by a crash can
    # be recovered (the backup itself only reads); waits up to 30 s for a writer to finish
    return sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)


def backup_database(db_path: Path, target: Path) -> Path:
    """Online backup to ``target`` (written to a temporary file first, then renamed)."""
    tmp = target.with_name(target.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        with closing(_open_source(db_path)) as src, closing(sqlite3.connect(tmp)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")  # a single self-contained file
            result = dst.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise BackupError(f"Integritätsprüfung der Sicherung fehlgeschlagen: {result}")
        os.replace(tmp, target)
    except sqlite3.Error as exc:
        raise BackupError(f"Sicherung fehlgeschlagen: {exc}") from None
    finally:
        tmp.unlink(missing_ok=True)
    return target


def export_decisions(db_path: Path, target: Path) -> int:
    """Writes the table ``assign_decisions`` as JSON; returns the number of decisions."""
    try:
        with closing(_open_source(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in conn.execute("SELECT * FROM assign_decisions ORDER BY decided_at, source_key")]
    except sqlite3.Error as exc:
        raise BackupError(f"Zuordnungen nicht lesbar: {exc}") from None
    data = {"format": "projektakte-zuordnungen/1", "version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(rows), "decisions": rows}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, target)
    return len(rows)


def run_backup(db_path: Path, target_dir: Path) -> dict:
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Zielverzeichnis nicht anlegbar: {target_dir} ({exc.strerror})") from None
    stamp = _timestamp()
    database = backup_database(db_path, target_dir / f"projektakte-{stamp}.sqlite3")
    decisions = target_dir / f"zuordnungen-{stamp}.json"
    count = export_decisions(db_path, decisions)
    return {"database": database, "decisions": decisions, "count": count, "size": database.stat().st_size}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m projektakte.backup",
                                 description="Konsistente Sicherung der Datenbank (im laufenden Betrieb möglich)")
    ap.add_argument("ziel", type=Path, help="Zielverzeichnis für die Sicherungsdateien")
    ap.add_argument("--db", type=Path, help="Datenbankdatei (Standard: DATA_DIR/projektakte.sqlite3)")
    a = ap.parse_args(argv)
    db_path = a.db or load_settings().db_path
    try:
        result = run_backup(db_path, a.ziel)
    except BackupError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    size_mb = f"{result['size'] / 1_048_576:.1f}".replace(".", ",")
    print(f"Datenbank gesichert: {result['database']} ({size_mb} MB)")
    print(f"Zuordnungen exportiert: {result['decisions']} ({result['count']} Entscheidungen)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
