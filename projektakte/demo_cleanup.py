"""Remove the generated demo data set from the index when switching from DEMO_MODE to real data.

The demo projects and mails are invented. Once the app runs on real sources (DEMO_MODE=0), they
must not show up in search and answers any more – also not through demo mails, which a missing or
empty real MAIL_DIR would otherwise keep forever (nothing is deleted then, see eml_source).

* A run with DEMO_MODE=1 records in ``meta`` that demo data was active.
* The first run with DEMO_MODE=0 afterwards – or any such run that still finds projects stored below
  DATA_DIR/demo (demo data indexed by an older version) – removes the projects below DATA_DIR/demo
  with their files and the .eml mails imported from DATA_DIR/demo/mails. Human decisions stay in
  ``assign_decisions``; the demo files themselves are not touched.
* ``python -m projektakte.index --purge-demo`` does the same on request.

Nothing is removed while PROJECTS_DIR or MAIL_DIR point into DATA_DIR/demo themselves: the demo
data is then the configured source.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path

from . import db
from .documents import remove_project_documents
from .mails.store import delete_mail, prune_cache_files, sweep_cache

log = logging.getLogger("projektakte.demo_cleanup")

DEMO_META = "demo_data"  # JSON: demo data set was indexed (DEMO_MODE=1) – since when, from where


def _roots(path: Path | str) -> list[Path]:
    """The folder as given (made absolute) and with links resolved."""
    p = Path(path)
    roots = [p.absolute()]
    try:
        roots.append(p.resolve())
    except (OSError, RuntimeError):
        pass
    return roots


def _below(path: Path | str, roots: list[Path], *, resolve: bool = True) -> bool:
    candidates = _roots(path) if resolve else [Path(path).absolute()]
    return any(c.is_relative_to(r) for c in candidates for r in roots)


def remember_demo(conn: sqlite3.Connection, demo_dir: Path) -> None:
    """Record that a run with DEMO_MODE=1 indexed the demo data set (the first time only)."""
    if db.get_meta(conn, DEMO_META) is None:
        db.set_meta(conn, DEMO_META, json.dumps({
            "dir": str(Path(demo_dir).absolute()),
            "since": datetime.now(timezone.utc).isoformat(timespec="seconds")}))


def demo_left_over(conn: sqlite3.Connection, settings) -> bool:
    """Is there demo data to remove in a run with DEMO_MODE=0? Either a DEMO_MODE=1 run was recorded,
    or projects below DATA_DIR/demo are stored although PROJECTS_DIR lies elsewhere. Never while both
    PROJECTS_DIR and MAIL_DIR point into DATA_DIR/demo (the demo data is the configured source)."""
    roots = _roots(settings.demo_dir)
    demo_projects = _below(settings.projects_dir, roots)
    if demo_projects and _below(settings.mail_dir, roots):
        return False
    if db.get_meta(conn, DEMO_META) is not None:
        return True
    # (a demo project kept as inactive because real mails refer to it has no files left: not counted)
    rows = conn.execute("SELECT path FROM projects WHERE active = 1 OR number IN "
                        "(SELECT project FROM documents WHERE mail_id IS NULL)")
    return not demo_projects and any(_below(r[0], roots, resolve=False) for r in rows)


def _message_id(path: Path) -> str | None:
    """Message-ID of an .eml file, read like mails.model.parse_eml reads it (headers only)."""
    with open(path, "rb") as fh:
        msg = BytesHeaderParser(policy=policy.default).parse(fh)
    return str(msg.get("Message-ID") or "").strip() or None


def _demo_mails(mail_root: Path) -> dict[str, str | None]:
    """Demo mail files (path below ``mail_root``) → Message-ID: read from the files, or computed by the
    demo generator if the demo folder was deleted meanwhile."""
    if not mail_root.is_dir():
        from .demo.generate import mail_message_ids

        return dict(mail_message_ids())
    out: dict[str, str | None] = {}
    for f in sorted(mail_root.rglob("*.eml")):
        try:
            if f.is_file() and not f.is_symlink():
                out[f.relative_to(mail_root).as_posix()] = _message_id(f)
        except OSError:
            continue
    return out


def _purge_mails(conn: sqlite3.Connection, mail_root: Path) -> tuple[int, list[str]]:
    """Remove .eml mails imported from a demo mail file: same path below the mail folder and same
    Message-ID (a real mail that merely has the same file name stays). Returns the number of mails
    and the cache files of their attachments (deleted after the commit)."""
    demo = _demo_mails(mail_root)
    removed, files = 0, []
    for row in conn.execute("SELECT id, source_key, message_id FROM mails WHERE source = 'eml'").fetchall():
        rel = Path(row["source_key"].removeprefix("eml:")).as_posix()
        if rel not in demo or demo[rel] != (row["message_id"] or None):
            continue
        files += delete_mail(conn, row["id"])  # sections, attachments, newsletter marker – decisions stay
        conn.execute("DELETE FROM meta WHERE key = ?", ("eml_stamp:" + row["source_key"],))  # eml_source stamp
        removed += 1
    return removed, files


def _purge_projects(conn: sqlite3.Connection, demo_dir: Path) -> tuple[int, int]:
    """Remove the projects stored below ``demo_dir`` with their files. A project that real mails
    still refer to stays known as inactive. Returns (projects, documents)."""
    roots = _roots(demo_dir)
    projects = documents = 0
    for row in conn.execute("SELECT number, path FROM projects").fetchall():
        if not _below(row["path"], roots):
            continue
        number = row["number"]
        documents += remove_project_documents(conn, number)
        if conn.execute("SELECT 1 FROM mails WHERE project = ? OR suggested_project = ? LIMIT 1",
                        (number, number)).fetchone():
            conn.execute("UPDATE projects SET active = 0, missing_since = COALESCE(missing_since, datetime('now')), "
                         "updated_at = datetime('now') WHERE number = ?", (number,))
        else:
            conn.execute("DELETE FROM projects WHERE number = ?", (number,))
        projects += 1
    return projects, documents


def purge_demo_data(conn: sqlite3.Connection, settings) -> dict:
    """Remove demo projects (with their files) and demo mails from the index; commits. Returns the
    counts; ``note`` says what was kept because a source folder points into DATA_DIR/demo."""
    demo_dir = Path(settings.demo_dir)
    roots = _roots(demo_dir)
    keep_projects = _below(settings.projects_dir, roots)
    keep_mails = _below(settings.mail_dir, roots)
    stats: dict = {"projects": 0, "documents": 0, "mails": 0, "cache_files": 0}
    files: list[str] = []
    if not keep_mails:
        stats["mails"], files = _purge_mails(conn, demo_dir / "mails")
    if not keep_projects:
        stats["projects"], stats["documents"] = _purge_projects(conn, demo_dir)
    if keep_projects or keep_mails:  # the marker stays: switching to real folders later cleans up
        which = " und ".join(n for n, keep in (("PROJECTS_DIR", keep_projects), ("MAIL_DIR", keep_mails)) if keep)
        stats["note"] = f"{which} zeigt in den Demo-Ordner {demo_dir} – diese Demo-Daten bleiben als Quelle erhalten"
        log.info(stats["note"])
    else:
        conn.execute("DELETE FROM meta WHERE key = ?", (DEMO_META,))
    conn.commit()
    cache_dir = getattr(settings, "cache_dir", None)
    if cache_dir is not None and not keep_mails:
        # the attachment files of the removed mails, and files left behind by older versions
        stats["cache_files"] = (prune_cache_files(conn, cache_dir, files)
                                + (sweep_cache(conn, cache_dir, every=None) or 0))
    note = demo_purge_note(stats)
    if note:
        log.warning(note)
    return stats


def demo_purge_note(stats: dict | None) -> str | None:
    """German note on a demo purge for log and status page (``last_warnings``), None if nothing was
    removed."""
    if not isinstance(stats, dict) or not (stats.get("projects") or stats.get("mails")):
        return None
    note = (f"Demo-Daten aus dem Index entfernt (DEMO_MODE=0): {stats.get('projects', 0)} Projekt(e) mit "
            f"{stats.get('documents', 0)} Dokument(en), {stats.get('mails', 0)} Mail(s)")
    if stats.get("cache_files"):
        note += f", {stats['cache_files']} Anhang-Datei(en) im Cache"
    return note
