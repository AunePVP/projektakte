"""Project register: projects are folders whose name starts with a project number."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .documents import is_inside, refresh_fts_titles, remove_project_documents, warn_once

log = logging.getLogger("projektakte.registry")


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
            if p.name.startswith(".") or not p.is_dir():
                continue
            if p.is_symlink() and not is_inside(p, boundary):
                warn_once(log, "Verknüpfung auf Ziel außerhalb von PROJECTS_DIR übersprungen: %s", p)
                continue
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

    Projects whose folder disappeared are marked inactive, not deleted (their mails and decisions
    stay), but their files leave the index. Projects below a folder that could not be listed
    (``unreadable``) are left alone. A new name refreshes the full-text titles of all sections."""
    existing = {r["number"]: r for r in conn.execute("SELECT * FROM projects")}
    added = updated = 0
    for f in folders:
        row = existing.get(f.number)
        if row is None:
            conn.execute("INSERT INTO projects(number, name, path) VALUES(?, ?, ?)",
                         (f.number, f.name, str(f.path)))
            added += 1
        elif (row["name"], row["path"], row["active"]) != (f.name, str(f.path), 1):
            conn.execute("UPDATE projects SET name=?, path=?, active=1, updated_at=datetime('now') "
                         "WHERE number=?", (f.name, str(f.path), f.number))
            if row["name"] != f.name:
                refresh_fts_titles(conn, f.number, f.name)
            updated += 1
    seen = {f.number for f in folders}
    blocked = [Path(p) for p in unreadable]
    gone = [n for n, r in existing.items() if n not in seen and r["active"]
            and not any(Path(r["path"]).is_relative_to(b) for b in blocked)]
    removed = 0
    for n in gone:
        conn.execute("UPDATE projects SET active=0, updated_at=datetime('now') WHERE number=?", (n,))
        removed += remove_project_documents(conn, n)
    return {"projects": len(folders), "added": added, "updated": updated, "deactivated": len(gone),
            "documents_removed": removed}


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
