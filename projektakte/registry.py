"""Project register: projects are folders whose name starts with a project number."""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_PROJECT_FOLDER_DEPTH, PROJECT_FOLDER_DEPTH_MAX, pattern_problem
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
    path: Path  # the real path (for reading); names may hold surrogate escapes, see printable()


# ------------------------------------------------------------------------------------- names
# A share mounted without iocharset=utf8 (SMB) delivers names that are no valid UTF-8; Python keeps
# their bytes as lone surrogates (PEP 383). Such names can be read with the real path but never be
# written to the database, a JSON answer or a UTF-8 terminal: they are shown and stored with U+FFFD.
ENCODING_HINT = "Freigabe mit iocharset=utf8 einbinden"


def has_bad_encoding(text: str) -> bool:
    """Does a file or folder name hold bytes that are no valid UTF-8 (surrogate escapes)?"""
    return any("\ud800" <= c <= "\udfff" for c in text)


def printable(text: str) -> str:
    """A file or folder name for display and storage: every byte that is no valid UTF-8 as �."""
    if not has_bad_encoding(text):
        return text
    return "".join("\ufffd" if "\ud800" <= c <= "\udfff" else c for c in text)


# ------------------------------------------------------------------------------------- expressions
def _split_alternatives(p: str) -> list[str]:
    """The top-level alternatives of an expression ('A|B(C|D)' → ['A', 'B(C|D)'])."""
    parts, depth, start, i, in_class = [], 0, 0, 0, False
    while i < len(p):
        c = p[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
            if p[i + 1:i + 2] == "]":
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            parts.append(p[start:i])
            start = i + 1
        i += 1
    return parts + [p[start:]]


def _group_end(p: str, start: int) -> int:
    """Index after the group that opens at ``p[start]`` ('('), or -1 if it is not closed."""
    depth, i, in_class = 0, start, False
    while i < len(p):
        c = p[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
            if p[i + 1:i + 2] == "]":
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return -1


def _unwrap(pattern: str) -> str:
    p = pattern.strip()
    while p.startswith("(?:") and p.endswith(")") and _group_end(p, 0) == len(p):
        p = p[3:-1]
    return p


def alternatives(pattern: str) -> frozenset[str]:
    """The top-level alternatives of an expression: '(?:A|B)' and 'B|A' → {A, B}; one scheme → {it}."""
    return frozenset(a.strip() for a in _split_alternatives(_unwrap(pattern)))


def loose_pattern(pattern: str) -> str | None:
    """The expression without its guards – the negative lookaheads at the start of an alternative,
    e.g. (?!(?:19|20)\\d\\d(?!\\d))\\d{4} → \\d{4} – or None if it has none. Matches of the loose
    expression that the real one refuses are the folders a guard excludes."""
    parts, changed = [], False
    for alt in _split_alternatives(_unwrap(pattern)):
        alt = alt.strip()
        while alt.startswith("(?!") and (end := _group_end(alt, 0)) > 0:
            alt, changed = alt[end:], True
        parts.append(alt)
    if not changed or any(not a for a in parts):
        return None
    return parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"


def number_regex(pattern: str) -> re.Pattern:
    """Find project numbers in free text (not glued to other letters/digits)."""
    return re.compile(rf"(?<![0-9A-Za-z])(?:{pattern})(?![0-9A-Za-z])", re.IGNORECASE)


def project_head(pattern: str) -> re.Pattern:
    """A folder name that starts with a project number: ``num`` is the number, ``name`` the rest
    (separators after the number dropped). Raises ``re.error`` for an unusable pattern."""
    return re.compile(rf"^(?P<num>{pattern})(?![0-9A-Za-z])[\s_\-–—:]*(?P<name>.*)$", re.IGNORECASE)


def clamp_depth(depth: int | None) -> int:
    """A folder depth within 1 … PROJECT_FOLDER_DEPTH_MAX (None: the default)."""
    if depth is None:
        return DEFAULT_PROJECT_FOLDER_DEPTH
    return max(1, min(PROJECT_FOLDER_DEPTH_MAX, int(depth)))


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


def scan_projects(root: Path, pattern: str, max_depth: int = DEFAULT_PROJECT_FOLDER_DEPTH, *,
                  unreadable: list[Path] | None = None,
                  duplicates: list[tuple[str, Path, Path]] | None = None,
                  excluded: list[tuple[str, Path]] | None = None,
                  bad_names: list[Path] | None = None) -> list[ProjectFolder]:
    """Find project folders below ``root`` (e.g. ``2024/BS-2024-0137 Grundschule …``).

    A folder whose name starts with a project number is a project; its subfolders are not searched
    for further projects. Other folders are searched down to ``max_depth`` levels below ``root``
    (PROJECT_FOLDER_DEPTH: 1 = only directly in ``root``). Hidden folders and links pointing outside
    ``root`` are skipped. Folders that cannot be listed are appended to ``unreadable``; a second folder
    with a number already found is skipped – logged, or appended to ``duplicates`` as (number, first
    folder, skipped folder) if that list is given. ``excluded`` receives (number, folder) of the folders
    a guard of the pattern keeps out (a year 1999 for (?!(?:19|20)\\d\\d(?!\\d))\\d{4}) although they
    lie at the level of project folders and hold none – no guard may drop projects silently.
    ``bad_names`` receives the folders whose name is no valid UTF-8. Nothing is ever written.
    """
    head = project_head(pattern)
    loose_text = loose_pattern(pattern) if excluded is not None else None
    loose = project_head(loose_text) if loose_text else None
    max_depth = clamp_depth(max_depth)
    found: dict[str, ProjectFolder] = {}
    found_depths: set[int] = set()
    guarded: list[tuple[str, Path, int]] = []
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
            if bad_names is not None and has_bad_encoding(child.name):
                bad_names.append(child)
            m = head.match(child.name)
            if m:
                number = m.group("num").upper()
                if number in found:
                    if duplicates is None:
                        log.warning("Projektnummer %s doppelt: %s und %s", number, printable(str(found[number].path)),
                                    printable(str(child)))
                    else:
                        duplicates.append((number, found[number].path, child))
                    continue
                found[number] = ProjectFolder(number, m.group("name").strip() or number, child)
                found_depths.add(depth)
                continue
            if loose is not None and (lm := loose.match(child.name)):
                guarded.append((lm.group("num").upper(), child, depth))
            if depth < max_depth:
                walk(child, depth + 1)

    walk(root, 1)
    if guarded and excluded is not None:
        holding = {a for p in found.values() for a in p.path.parents}
        excluded.extend((number, folder) for number, folder, depth in guarded
                        if depth in found_depths and folder not in holding)
    return sorted(found.values(), key=lambda p: p.number)


def sync_projects(conn: sqlite3.Connection, folders: list[ProjectFolder], *,
                  unreadable: list[Path] | tuple = ()) -> dict:
    """Store scanned projects and keep the derived data in step.

    A project whose folder disappeared is marked inactive, not deleted (its mails and decisions
    stay). Its files are hidden from search and answers at once and deleted only after the grace
    period (:data:`MISSING_GRACE` and :data:`MISSING_RUNS`); a folder that comes back before brings
    them back as they were. Projects below a folder that could not be listed (``unreadable``) are
    left alone. A new name refreshes the full-text titles of all sections. Names that are no valid
    UTF-8 are stored with � (:func:`printable`) – the database only holds valid text."""
    existing = {r["number"]: r for r in conn.execute("SELECT * FROM projects")}
    now = _utcnow()
    stats = {"projects": len(folders), "added": 0, "updated": 0, "reactivated": 0, "deactivated": 0,
             "documents_hidden": 0, "documents_restored": 0, "documents_removed": 0}
    seen: set[str] = set()
    for f in folders:
        number, name, path = printable(f.number), printable(f.name), printable(str(f.path))
        if number in seen:  # two numbers that differ only in bytes that are no UTF-8
            continue
        seen.add(number)
        row = existing.get(number)
        if row is None:
            conn.execute("INSERT INTO projects(number, name, path) VALUES(?, ?, ?)", (number, name, path))
            stats["added"] += 1
            continue
        if (row["name"], row["path"], row["active"]) == (name, path, 1):
            continue
        conn.execute("UPDATE projects SET name=?, path=?, active=1, missing_since=NULL, missing_runs=0, "
                     "updated_at=datetime('now') WHERE number=?", (name, path, number))
        if not row["active"]:
            restored = restore_project_documents(conn, number, name)
            stats["reactivated"] += 1
            stats["documents_restored"] += restored
            log.info("Projektordner wieder vorhanden: %s – %d Dokument(e) wieder sichtbar", number, restored)
        if row["name"] != name:
            refresh_fts_titles(conn, number, name)
        stats["updated"] += 1
    blocked = [Path(printable(str(p))) for p in unreadable]
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


def unchanged_stats(active: int, problem: str) -> dict:
    """Stats of a project sync that was skipped (``warning``: German, for log and status page)."""
    return {"projects": active, "added": 0, "updated": 0, "deactivated": 0, "documents_removed": 0,
            "warning": f"{problem}; Projekte und Dokumente wurden nicht abgeglichen"}


def active_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM projects WHERE active = 1").fetchone()[0]


def sync_from_folder(conn: sqlite3.Connection, root: Path, pattern: str, *,
                     max_depth: int = DEFAULT_PROJECT_FOLDER_DEPTH) -> dict:
    """Scan PROJECTS_DIR and store the result – unless the folder looks unavailable.

    A missing or unreadable folder, or one that suddenly contains no projects although active
    projects are known (share not mounted yet), changes nothing: the stats then carry ``warning``
    and the caller skips the documents as well. So does an unusable ``pattern``."""
    active = active_count(conn)
    if bad := pattern_problem(pattern):
        problem = f"PROJECT_NUMBER_PATTERN={pattern} ist unbrauchbar: {bad}"
        log.warning("%s – Projekte und Dokumente bleiben unverändert", problem)
        return unchanged_stats(active, problem)
    problem = projects_root_problem(root)
    unreadable: list[Path] = []
    excluded: list[tuple[str, Path]] = []
    bad_names: list[Path] = []
    folders = [] if problem else scan_projects(root, pattern, max_depth, unreadable=unreadable,
                                               excluded=excluded, bad_names=bad_names)
    if not problem and not folders and active:
        problem = (f"Projektordner {root} enthält keine Projekte mehr (bisher {active} aktiv) – "
                   "Laufwerk nicht eingebunden?")
    if problem:
        log.warning("%s – Projekte und Dokumente bleiben unverändert", problem)
        return unchanged_stats(active, problem)
    stats = sync_projects(conn, folders, unreadable=unreadable)
    if unreadable:
        stats["unreadable"] = [printable(str(p)) for p in unreadable]
    # what the scan noticed (structure.record_scan_notes shows it on the status page and logs it once)
    root_path = Path(root)
    if excluded:
        stats["guard_excluded"] = {"count": len(excluded), "examples": [
            printable(_relative(folder, root_path)) for _, folder in excluded[:MAX_NOTED]]}
    if bad_names:
        stats["bad_names"] = {"count": len(bad_names), "examples": [
            printable(_relative(folder, root_path)) for folder in bad_names[:MAX_NOTED]]}
    return stats


MAX_NOTED = 8  # examples kept of the folders a scan noticed (guard, encoding)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


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
