"""Project register: projects are folders whose name starts with a project number."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .documents import (_sqlite_time, hide_project_documents, link_problem, refresh_fts_titles,
                        remove_project_documents, restore_project_documents, warn_once)

log = logging.getLogger("projektakte.registry")

# A project folder that disappears may only be unreachable for a while (network share, a year folder
# briefly gone, a folder renamed and renamed back). Its files leave search and answers at once but
# are deleted only once it has been missing for MISSING_GRACE *and* in MISSING_RUNS consecutive
# passes; if it comes back sooner, they return without being extracted or embedded again.
MISSING_GRACE = timedelta(hours=24)
MISSING_RUNS = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProjectFolder:
    number: str
    name: str
    path: Path


def number_regex(pattern: str) -> re.Pattern:
    """Find project numbers in free text (not glued to other letters/digits)."""
    return re.compile(rf"(?<![0-9A-Za-z])(?:{pattern})(?![0-9A-Za-z])", re.IGNORECASE)


def projects_root_problem(root: Path) -> str | None:
    """Why PROJECTS_DIR cannot be read right now (German, for log and status) – or None."""
    root = Path(root)
    try:
        if not root.exists():
            return f"Projektordner {root} nicht vorhanden (Laufwerk nicht eingebunden?)"
        if not root.is_dir():
            return f"Projektordner {root} ist kein Ordner"
        with os.scandir(root) as entries:
            next(entries, None)
    except OSError as exc:
        return f"Projektordner {root} nicht lesbar ({exc.strerror or exc})"
    return None


def scan_projects(root: Path, pattern: str, max_depth: int = 2, *,
                  unreadable: list[Path] | None = None) -> list[ProjectFolder]:
    """Find project folders below ``root`` (e.g. ``2024/BS-2024-0137 Grundschule …``).

    A folder whose name starts with a project number is a project; its subfolders are not searched
    for further projects. Hidden folders and links pointing outside ``root`` are skipped. Folders
    that cannot be listed are appended to ``unreadable``. Nothing is ever written.
    """
    head = re.compile(rf"^(?P<num>{pattern})(?![0-9A-Za-z])[\s_\-–—:]*(?P<name>.*)$", re.IGNORECASE)
    found: dict[str, ProjectFolder] = {}
    root = Path(root)
    if not root.is_dir():
        log.warning("Projektordner %s existiert nicht", root)
        return []
    boundary = root.resolve()

    def subfolders(folder: Path) -> list[Path]:
        out = []
        for p in folder.iterdir():
            if p.name.startswith("."):
                continue
            if p.is_symlink() and (problem := link_problem(p, boundary)):
                warn_once(log, "%s: %s", problem, p)
                continue
            if p.is_dir():
                out.append(p)
        return sorted(out)

    def walk(folder: Path, depth: int) -> None:
        try:
            children = subfolders(folder)
        except OSError as exc:
            log.warning("Ordner nicht lesbar: %s (%s)", folder, exc)
            if unreadable is not None:
                unreadable.append(folder)
            return
        for child in children:
            m = head.match(child.name)
            if m:
                number = m.group("num").upper()
                if number in found:
                    log.warning("Projektnummer %s doppelt: %s und %s", number, found[number].path, child)
                    continue
                found[number] = ProjectFolder(number, m.group("name").strip() or number, child)
            elif depth < max_depth:
                walk(child, depth + 1)

    walk(root, 1)
    return sorted(found.values(), key=lambda p: p.number)


def sync_projects(conn: sqlite3.Connection, folders: list[ProjectFolder], *,
                  unreadable: list[Path] | tuple = ()) -> dict:
    """Store scanned projects and keep the derived data in step.

    A project whose folder disappeared is marked inactive, not deleted (its mails and decisions
    stay). Its files are hidden from search and answers at once and deleted only after the grace
    period (:data:`MISSING_GRACE` and :data:`MISSING_RUNS`); a folder that comes back before brings
    them back as they were. Projects below a folder that could not be listed (``unreadable``) are
    left alone. A new name refreshes the full-text titles of all sections."""
    existing = {r["number"]: r for r in conn.execute("SELECT * FROM projects")}
    now = _utcnow()
    stats = {"projects": len(folders), "added": 0, "updated": 0, "reactivated": 0, "deactivated": 0,
             "documents_hidden": 0, "documents_restored": 0, "documents_removed": 0}
    for f in folders:
        row = existing.get(f.number)
        if row is None:
            conn.execute("INSERT INTO projects(number, name, path) VALUES(?, ?, ?)",
                         (f.number, f.name, str(f.path)))
            stats["added"] += 1
            continue
        if (row["name"], row["path"], row["active"]) == (f.name, str(f.path), 1):
            continue
        conn.execute("UPDATE projects SET name=?, path=?, active=1, missing_since=NULL, missing_runs=0, "
                     "updated_at=datetime('now') WHERE number=?", (f.name, str(f.path), f.number))
        if not row["active"]:
            restored = restore_project_documents(conn, f.number, f.name)
            stats["reactivated"] += 1
            stats["documents_restored"] += restored
            log.info("Projektordner wieder vorhanden: %s – %d Dokument(e) wieder sichtbar", f.number, restored)
        if row["name"] != f.name:
            refresh_fts_titles(conn, f.number, f.name)
        stats["updated"] += 1
    seen = {f.number for f in folders}
    blocked = [Path(p) for p in unreadable]
    cutoff = _sqlite_time(now - MISSING_GRACE)
    for number, row in existing.items():
        if number in seen or any(Path(row["path"]).is_relative_to(b) for b in blocked):
            continue
        if row["active"]:  # just went missing: hide its files, start the grace period
            conn.execute("UPDATE projects SET active=0, missing_since=?, missing_runs=1, "
                         "updated_at=datetime('now') WHERE number=?", (_sqlite_time(now), number))
            hidden = hide_project_documents(conn, number)
            stats["deactivated"] += 1
            stats["documents_hidden"] += hidden
            log.warning("Projektordner fehlt: %s (%s) – %d Dokument(e) aus Suche und Antworten ausgeblendet; "
                        "gelöscht werden sie erst, wenn der Ordner %d h lang und in %d Läufen fehlt",
                        number, row["path"], hidden, MISSING_GRACE // timedelta(hours=1), MISSING_RUNS)
            continue
        if row["missing_since"] is None:  # inactive since an older version – its files are gone
            continue
        runs = row["missing_runs"] + 1
        conn.execute("UPDATE projects SET missing_runs=? WHERE number=?", (runs, number))
        if runs >= MISSING_RUNS and row["missing_since"] <= cutoff:
            removed = remove_project_documents(conn, number)
            if removed:
                log.info("Projektordner %s fehlt seit %s (UTC) – %d Dokument(e) aus dem Index entfernt",
                         number, row["missing_since"], removed)
            stats["documents_removed"] += removed
    return stats


def sync_from_folder(conn: sqlite3.Connection, root: Path, pattern: str) -> dict:
    """Scan PROJECTS_DIR and store the result – unless the folder looks unavailable.

    A missing or unreadable folder, or one that suddenly contains no projects although active
    projects are known (share not mounted yet), changes nothing: the stats then carry ``warning``
    and the caller skips the documents as well."""
    active = conn.execute("SELECT COUNT(*) FROM projects WHERE active = 1").fetchone()[0]
    problem = projects_root_problem(root)
    unreadable: list[Path] = []
    folders = [] if problem else scan_projects(root, pattern, unreadable=unreadable)
    if not problem and not folders and active:
        problem = (f"Projektordner {root} enthält keine Projekte mehr (bisher {active} aktiv) – "
                   "Laufwerk nicht eingebunden?")
    if problem:
        log.warning("%s – Projekte und Dokumente bleiben unverändert", problem)
        return {"projects": active, "added": 0, "updated": 0, "deactivated": 0, "documents_removed": 0,
                "warning": f"{problem}; Projekte und Dokumente wurden nicht abgeglichen"}
    stats = sync_projects(conn, folders, unreadable=unreadable)
    if unreadable:
        stats["unreadable"] = [str(p) for p in unreadable]
    return stats


def list_projects(conn: sqlite3.Connection, q: str = "", include_inactive: bool = False) -> list[dict]:
    sql = "SELECT number, name, path, active FROM projects"
    where, args = [], []
    if not include_inactive:
        where.append("active = 1")
    if q.strip():
        where.append("(number LIKE ? OR name LIKE ?)")
        args += [f"%{q.strip()}%"] * 2
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY number DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def get_project(conn: sqlite3.Connection, number: str) -> dict | None:
    row = conn.execute("SELECT number, name, path, active FROM projects WHERE number = ? COLLATE NOCASE",
                       (number,)).fetchone()
    return dict(row) if row else None
