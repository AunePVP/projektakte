"""Project register: projects are folders whose name starts with a project number."""
from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("projektakte.registry")


@dataclass(frozen=True)
class ProjectFolder:
    number: str
    name: str
    path: Path


def number_regex(pattern: str) -> re.Pattern:
    """Find project numbers in free text (not glued to other letters/digits)."""
    return re.compile(rf"(?<![0-9A-Za-z])(?:{pattern})(?![0-9A-Za-z])", re.IGNORECASE)


def scan_projects(root: Path, pattern: str, max_depth: int = 2) -> list[ProjectFolder]:
    """Find project folders below ``root`` (e.g. ``2024/BS-2024-0137 Grundschule …``).

    A folder whose name starts with a project number is a project; its subfolders are not searched
    for further projects. Hidden folders are skipped. Nothing is ever written.
    """
    head = re.compile(rf"^(?P<num>{pattern})(?![0-9A-Za-z])[\s_\-–—:]*(?P<name>.*)$", re.IGNORECASE)
    found: dict[str, ProjectFolder] = {}
    root = Path(root)
    if not root.is_dir():
        log.warning("Projektordner %s existiert nicht", root)
        return []

    def walk(folder: Path, depth: int) -> None:
        try:
            children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
        except OSError as exc:
            log.warning("Ordner nicht lesbar: %s (%s)", folder, exc)
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


def sync_projects(conn: sqlite3.Connection, folders: list[ProjectFolder]) -> dict:
    """Store scanned projects; projects whose folder disappeared are marked inactive, not deleted."""
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
            updated += 1
    seen = {f.number for f in folders}
    gone = [n for n, r in existing.items() if n not in seen and r["active"]]
    for n in gone:
        conn.execute("UPDATE projects SET active=0, updated_at=datetime('now') WHERE number=?", (n,))
    return {"projects": len(folders), "added": added, "updated": updated, "deactivated": len(gone)}


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
