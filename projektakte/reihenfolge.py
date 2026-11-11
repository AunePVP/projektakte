"""Order of the documents pass ("Reihenfolge" on the status page) and the state of each project in it.

A pass takes the active projects in this order:

1. projects a person moved to the front ("Vorziehen", ``projects.pinned``), in the order they were pinned;
2. projects not completely read yet with the current extraction profile (the queue, "Warteschlange"),
   folder by folder – the top-level folders under PROJECTS_DIR in the order set on the status page
   (``index_folders``), else INDEX_FOLDER_ORDER, then the remaining folders (:func:`default_key`: numbered
   ones highest first, then by name) – inside a folder the highest (newest) number first;
3. projects already done, in the same order: they are walked again in every pass, so new and changed files
   (and files of further folders) are still found in every run, as before – unchanged files cost a stat.

A folder switched off ("aus") is not read at all: its projects are in no pass (not even pinned ones), their
scan pages do not go to the model, a project of it being read when it is switched off stops after the file
it is reading. What was read before stays in the index and searchable – nothing is deleted, and the project
register is unchanged (the projects stay active). Mails and their attachments are not affected.

A site (Bauvorhaben) and its orders stay together: the orders count in the site's top-level folder, are
sorted by the highest order number of the site, and the site's own files come right after its orders.

"Done" (``projects.done_at``) means: a documents pass over all folders of the project ended with the
current extraction profile (``done_profile``) – LLM_VISION switched on or Tesseract installed puts every
project back into the queue (another vision model does not: extract.profile_key) (only incomplete files are read again then). A file that has to be extracted again
(new, changed, due for a retry) clears ``done_at`` before it is read, so a project interrupted by a restart
is still in the queue afterwards and is taken first. ``done_skipped`` counts what the project could not read
(errors, files too large, names in a wrong encoding, folders not listable, documents stored with an error).

Changes made on the status page bump a counter in ``meta``; the running pass compares it between two
projects (one small query) and sorts the projects it has not started yet again. :data:`LIVE` holds what the
pass is doing right now for the status page (same process: the background indexer thread), with the counts
and times of the running pass for the estimates (zeiten.py).
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import db, zeiten
from .db import BAUVORHABEN
from .extract import profile_key

ROOT_FOLDER = ""  # projects that lie directly in PROJECTS_DIR
ROOT_LABEL = "(direkt im Projektordner)"
VERSION_KEY = "reihenfolge_version"
FOLDER_ACTIONS = ("hoch", "runter", "aus", "an")
OFF_CHECK_SECONDS = 1.0  # a project being read checks at most this often whether its folder was switched off
_ENTRY_COLUMNS = "number, name, path, parent, kind, done_at, done_profile, done_skipped, pinned"
_DIGITS = re.compile(r"(\d+)")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def natural_key(text: str) -> tuple:
    """Sort key comparing digit runs as numbers ("9" < "10", "2842" < "284201")."""
    return tuple((0, int(part), part) if part.isdigit() else (1, 0, part)
                 for part in _DIGITS.split(text.casefold()) if part)


def default_key(name: str) -> tuple:
    """Default order of the folders nobody placed: numbered folders highest first ("3", "2", "1", like the
    newest numbers first inside a folder), then the others by name, the projects directly in PROJECTS_DIR
    last."""
    if name == ROOT_FOLDER:
        return (2,)
    if name[:1].isdigit():
        return (0, _Desc(natural_key(name)))
    return (1, name.casefold())


class _Desc:
    """Wraps a key so that it sorts in reverse."""
    __slots__ = ("key",)

    def __init__(self, key) -> None:
        self.key = key

    def __lt__(self, other: "_Desc") -> bool:
        return other.key < self.key

    def __eq__(self, other) -> bool:
        return isinstance(other, _Desc) and other.key == self.key


def folder_label(name: str) -> str:
    return name if name != ROOT_FOLDER else ROOT_LABEL


def top_folder(path: str, projects_dir: Path | str | None) -> str:
    """The top-level folder under PROJECTS_DIR a project folder lies in ("" directly in it or outside)."""
    if not projects_dir:
        return ROOT_FOLDER
    for root in dict.fromkeys((str(projects_dir), os.path.realpath(projects_dir))):
        prefix = root.rstrip(os.sep) + os.sep
        if path.startswith(prefix):
            head, sep, _rest = path[len(prefix):].partition(os.sep)
            return head if sep else ROOT_FOLDER
    return ROOT_FOLDER


# -- counter of changes made by people ---------------------------------------------------------------
def version(conn: sqlite3.Connection) -> int:
    try:
        return int(db.get_meta(conn, VERSION_KEY, "0") or 0)
    except ValueError:
        return 0


def _bump(conn: sqlite3.Connection) -> None:
    db.set_meta(conn, VERSION_KEY, str(version(conn) + 1))


# -- the projects and their state --------------------------------------------------------------------
def entries(conn: sqlite3.Connection, projects_dir: Path | str | None, profile: str | None) -> list[dict]:
    """All active projects and sites with their top-level folder, group (a site and its orders) and whether
    they are done with ``profile`` – one query, no per-project lookups."""
    rows = [dict(r) for r in conn.execute(f"SELECT {_ENTRY_COLUMNS} FROM projects WHERE active = 1")]
    sites = {}
    for e in rows:
        e["folder"] = top_folder(e["path"], projects_dir)
        e["done"] = bool(e["done_at"]) and (profile is None or profile_key(e["done_profile"] or "") == profile_key(profile))
        e["group"] = e["number"]
        if e["kind"] == BAUVORHABEN:
            sites[e["number"]] = e
    for e in rows:  # an order of a site counts in the site's folder and stays next to it
        site = sites.get(e["parent"] or "") if e["kind"] != BAUVORHABEN else None
        if site is not None:
            e["folder"], e["group"] = site["folder"], site["number"]
    return rows


def folders(conn: sqlite3.Connection, present: Iterable[str], default: Iterable[str] = ()) -> list[dict]:
    """The top-level folders holding projects (``present``) in their reading order: as set on the status
    page, else ``default`` (INDEX_FOLDER_ORDER), then the others (:func:`default_key`); switched-off
    folders last. Each: ``name``, ``enabled``."""
    present = set(present)
    stored = conn.execute("SELECT name, enabled FROM index_folders ORDER BY position").fetchall()
    base = [(r["name"], bool(r["enabled"])) for r in stored] if stored else [(n, True) for n in default]
    out, seen = [], set()
    for name, enabled in base:
        if name in present and name not in seen:
            seen.add(name)
            out.append({"name": name, "enabled": enabled})
    out += [{"name": n, "enabled": True} for n in sorted(present - seen, key=default_key)]
    out.sort(key=lambda f: not f["enabled"])  # stable: switched-off folders at the end
    return out


def customized(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM index_folders LIMIT 1").fetchone() is not None


def ordered(items: list[dict], folder_list: list[dict]) -> list[dict]:
    """``items`` (from :func:`entries`) in the order of a pass (see the module docstring)."""
    rank = {f["name"]: i for i, f in enumerate(folder_list)}
    top: dict[str, tuple] = {}  # group → its highest order number (a site without orders: its own)
    for e in items:
        if e["kind"] != BAUVORHABEN:
            key = natural_key(e["number"])
            if e["group"] not in top or key > top[e["group"]]:
                top[e["group"]] = key
    for e in items:
        top.setdefault(e["group"], natural_key(e["number"]))
    out = sorted(items, key=lambda e: natural_key(e["number"]), reverse=True)  # newest first
    out.sort(key=lambda e: e["kind"] == BAUVORHABEN)  # a site after its orders
    out.sort(key=lambda e: (top[e["group"]], e["group"]), reverse=True)  # groups together, newest first
    last = len(rank)
    out.sort(key=lambda e: (0, e["pinned"], 0) if e["pinned"] is not None
             else (1, int(e["done"]), rank.get(e["folder"], last)))
    return out


def mark_off(items: list[dict], folder_list: list[dict]) -> set[str]:
    """Set ``off`` on the projects of folders switched off; returns their numbers."""
    off_folders = {f["name"] for f in folder_list if not f["enabled"]}
    off = set()
    for e in items:
        e["off"] = e["folder"] in off_folders
        if e["off"]:
            off.add(e["number"])
    return off


@dataclass
class View:
    """Everything the status page and /api/reihenfolge show, computed once per request."""
    order: list[dict]  # all projects in pass order
    folders: list[dict]  # with total / done counts
    customized: bool
    position: dict[str, int] = field(default_factory=dict)  # number → place in the queue (1 …)

    @property
    def queue(self) -> list[dict]:
        """What the next passes read: pinned and not done projects – none of a folder switched off."""
        return [e for e in self.order if not e["off"] and (not e["done"] or e["pinned"] is not None)]

    @property
    def switched_off(self) -> list[dict]:
        """Projects not done (or pinned) that are not read because their folder is switched off."""
        return [e for e in self.order if e["off"] and (not e["done"] or e["pinned"] is not None)]

    @property
    def done(self) -> list[dict]:
        return sorted((e for e in self.order if e["done"] and e["pinned"] is None),
                      key=lambda e: e["done_at"] or "", reverse=True)


def view(conn: sqlite3.Connection, projects_dir: Path | str | None, profile: str | None,
         default: Iterable[str] = ()) -> View:
    items = entries(conn, projects_dir, profile)
    folder_list = folders(conn, {e["folder"] for e in items}, default)
    mark_off(items, folder_list)
    order = ordered(items, folder_list)
    counts: dict[str, list[int]] = {}
    for e in items:
        if e["kind"] != BAUVORHABEN:  # "N von M Projekten" like the Bestand card: orders, no sites
            c = counts.setdefault(e["folder"], [0, 0])
            c[0] += 1
            c[1] += e["done"]
    place = 0
    for i, f in enumerate(folder_list):
        f["label"] = folder_label(f["name"])
        f["rank"] = i + 1 if f["enabled"] else None
        f["total"], f["finished"] = counts.get(f["name"], [0, 0])
    v = View(order=order, folders=folder_list, customized=customized(conn))
    for e in v.queue:
        place += 1
        v.position[e["number"]] = place
    return v


# -- changes made by people (status page, API) ------------------------------------------------------
class OrderError(ValueError):
    """A change that cannot be made (German message)."""


def change_folder(conn: sqlite3.Connection, projects_dir, default: Iterable[str], name: str, action: str) -> str:
    """Move a top-level folder up or down, or switch it off ("aus": its projects are not read) or on again.
    The whole order is stored from then on (INDEX_FOLDER_ORDER no longer applies). Returns a German note."""
    present = {e["folder"] for e in entries(conn, projects_dir, None)}
    current = folders(conn, present, default)
    names = [f["name"] for f in current]
    if name not in names:
        raise OrderError(f"Ordner „{folder_label(name)}“ enthält keine Projekte.")
    if action not in FOLDER_ACTIONS:
        raise OrderError(f"Unbekannte Aktion: {action}")
    i = names.index(name)
    f = current[i]
    if action in ("hoch", "runter"):
        j = i - 1 if action == "hoch" else i + 1
        if not 0 <= j < len(current) or current[j]["enabled"] != f["enabled"]:
            raise OrderError(f"Ordner „{folder_label(name)}“ lässt sich nicht weiter verschieben.")
        current[i], current[j] = current[j], current[i]
        note = f"Ordner {folder_label(name)} ist jetzt an Stelle {j + 1}."
    else:
        current.pop(i)
        f["enabled"] = action == "an"
        enabled = [c for c in current if c["enabled"]]
        current = [*enabled, f, *[c for c in current if not c["enabled"]]]
        note = (f"Ordner {folder_label(name)} ist wieder eingeschaltet – seine Projekte kommen in die "
                f"Warteschlange." if f["enabled"]
                else f"Ordner {folder_label(name)} ist ausgeschaltet – seine Projekte werden nicht gelesen. Schon "
                     f"Gelesenes bleibt durchsuchbar.")
    _store_folders(conn, current)
    return note


def set_folder_order(conn: sqlite3.Connection, projects_dir, default: Iterable[str], order: list[str],
                     off: Iterable[str] = ()) -> None:
    """The whole order at once (API): ``order`` first, the other folders after them as before; ``off``
    switched off."""
    present = {e["folder"] for e in entries(conn, projects_dir, None)}
    unknown = [n for n in [*order, *off] if n not in present]
    if unknown:
        raise OrderError("Ordner ohne Projekte: " + ", ".join(folder_label(n) for n in unknown))
    off = set(off)
    current = folders(conn, present, default)
    rest = [f["name"] for f in current if f["name"] not in order]
    items = [{"name": n, "enabled": n not in off} for n in dict.fromkeys([*order, *rest])]
    items.sort(key=lambda f: not f["enabled"])
    _store_folders(conn, items)


def _store_folders(conn: sqlite3.Connection, items: list[dict]) -> None:
    # folders without projects right now keep their place behind the others (they may come back)
    names = {f["name"] for f in items}
    kept = [(r["name"], r["enabled"]) for r in conn.execute("SELECT name, enabled FROM index_folders ORDER BY position")
            if r["name"] not in names]
    conn.execute("DELETE FROM index_folders")
    conn.executemany("INSERT INTO index_folders(name, position, enabled) VALUES(?, ?, ?)",
                     [(f["name"], i, int(f["enabled"])) for i, f in enumerate(items)]
                     + [(n, len(items) + i, enabled) for i, (n, enabled) in enumerate(kept)])
    _bump(conn)


def reset_folders(conn: sqlite3.Connection) -> None:
    """Back to INDEX_FOLDER_ORDER and the default order."""
    conn.execute("DELETE FROM index_folders")
    _bump(conn)


def _active(conn: sqlite3.Connection, number: str) -> sqlite3.Row:
    row = conn.execute("SELECT number, pinned FROM projects WHERE number = ? COLLATE NOCASE AND active = 1",
                       (number.strip(),)).fetchone()
    if row is None:
        raise OrderError(f"Projekt {number} nicht gefunden.")
    return row


def pin(conn: sqlite3.Connection, number: str, projects_dir=None, default: Iterable[str] = ()) -> str:
    """Move a project to the front: after the running one and the projects pinned before it. Not for a
    project of a folder switched off (it is not read at all: switch the folder on first)."""
    row = _active(conn, number)
    items = entries(conn, projects_dir, None)
    folder = next((e["folder"] for e in items if e["number"] == row["number"]), ROOT_FOLDER)
    if any(f["name"] == folder and not f["enabled"] for f in folders(conn, {e["folder"] for e in items}, default)):
        raise OrderError(f"{row['number']} liegt im ausgeschalteten Ordner {folder_label(folder)} und wird nicht "
                         f"gelesen – erst den Ordner einschalten.")
    if row["pinned"] is None:
        top = conn.execute("SELECT COALESCE(MAX(pinned), 0) FROM projects").fetchone()[0]
        conn.execute("UPDATE projects SET pinned = ? WHERE number = ?", (top + 1, row["number"]))
        _bump(conn)
    return f"{row['number']} vorgezogen – kommt als Nächstes dran."


def unpin(conn: sqlite3.Connection, number: str) -> str:
    row = _active(conn, number)
    if row["pinned"] is not None:
        conn.execute("UPDATE projects SET pinned = NULL WHERE number = ?", (row["number"],))
        _bump(conn)
    return f"{row['number']} wieder an seinem Platz."


# -- the pass ----------------------------------------------------------------------------------------
class Plan:
    """The order of one documents pass; :meth:`stale` tells when a person changed it meanwhile. Projects of
    folders switched off are left out (:meth:`order`), also when the folder is switched off during the pass
    (:meth:`switched_off`)."""

    def __init__(self, conn: sqlite3.Connection, projects_dir, default: Iterable[str], profile: str | None):
        self.conn, self.projects_dir, self.default, self.profile = conn, projects_dir, tuple(default), profile
        self.version = version(conn)
        self.folder_of: dict[str, str] = {}
        self.done: set[str] = set()  # projects done with ``profile`` when the order was last read
        self.off: set[str] = set()  # projects of folders switched off
        self._off_version = self.version
        self._off_checked = 0.0

    def stale(self) -> bool:
        return version(self.conn) != self.version

    def _read(self) -> list[dict]:
        items = entries(self.conn, self.projects_dir, self.profile)
        folder_list = folders(self.conn, {e["folder"] for e in items}, self.default)
        self.off = mark_off(items, folder_list)
        self.folder_of = {e["number"]: e["folder"] for e in items}
        self.done = {e["number"] for e in items if e["done"]}
        return ordered(items, folder_list)

    def rank(self) -> dict[str, int]:
        """number → place in the pass order (re-read from the database)."""
        self.version = self._off_version = version(self.conn)
        return {e["number"]: i for i, e in enumerate(self._read())}

    def order(self, projects: list[dict]) -> list[dict]:
        """``projects`` in the pass order, without those of folders switched off."""
        place = self.rank()
        last = len(place)
        return sorted((p for p in projects if p["number"] not in self.off), key=lambda p: place.get(p["number"], last))

    def queued(self, projects: list[dict]) -> int:
        """How many of ``projects`` are not done (the queue part of the pass)."""
        return sum(1 for p in projects if p["number"] not in self.done)

    def switched_off(self, number: str) -> bool:
        """Was the folder of ``number`` switched off since the order was read? Checked at most every
        OFF_CHECK_SECONDS (one small query; the folders are read again only after a change)."""
        now = time.monotonic()
        if now - self._off_checked < OFF_CHECK_SECONDS:
            return number in self.off
        self._off_checked = now
        current = version(self.conn)
        if current != self._off_version:
            self._off_version = current
            items = entries(self.conn, self.projects_dir, None)
            self.off = mark_off(items, folders(self.conn, {e["folder"] for e in items}, self.default))
        return number in self.off


FLUSH_FILES = 50  # files read between two updates of the stored average per file


class ProjectRun:
    """Follows the documents pass of one project (documents.index_project_documents): files listed and
    gone through (for the status page), files read and how long that took (for the estimates), what it could
    not read, whether a file had to be read – and whether its folder was switched off meanwhile."""

    def __init__(self, conn: sqlite3.Connection | None = None, number: str | None = None, *,
                 plan: Plan | None = None, queued: bool = False):
        self.conn, self.number, self.plan, self.queued = conn, number, plan, queued
        self.total = 0
        self.seen = 0
        self.problems = 0
        self.reachable = False
        self.stopped = False  # its folder was switched off: the project ends without being done
        self._changed = False
        self.started = time.monotonic()
        self.read = 0
        self.read_seconds = 0.0
        self._unflushed = (0, 0.0)

    def listed(self, n: int) -> None:
        self.reachable = True
        self.total += n
        LIVE.files(self.number, self.seen, self.total)

    def step(self) -> bool:
        """The next file: counted at once for "x von y Dateien". False if the project is to stop now (its
        folder was switched off)."""
        self.seen += 1
        LIVE.step(self.number, self.seen, self.total)
        if self.plan is not None and self.number and self.plan.switched_off(self.number):
            self.stopped = True
        return not self.stopped

    def problem(self, n: int = 1) -> None:
        self.problems += n

    def file_read(self, seconds: float) -> None:
        """A file was extracted and written, taking ``seconds``."""
        self.read += 1
        self.read_seconds += seconds
        LIVE.file_read(self.number, seconds)
        n, total = self._unflushed
        self._unflushed = (n + 1, total + seconds)
        if n + 1 >= FLUSH_FILES:
            self._flush()

    def _flush(self) -> None:
        n, total = self._unflushed
        self._unflushed = (0, 0.0)
        if self.conn is None or not n:
            return
        zeiten.add(self.conn, zeiten.FILE_KEY, total, n)
        self.conn.commit()

    def changed(self) -> None:
        """A file is about to be read: the project is not done until its pass ends."""
        if self._changed or self.conn is None:
            return
        self._changed = True
        self.conn.execute("UPDATE projects SET done_at = NULL WHERE number = ? AND done_at IS NOT NULL",
                          (self.number,))
        self.conn.commit()

    def finish(self, profile: str) -> None:
        """The pass over all folders ended: done (and no longer pinned) – unless no folder was reachable or
        the folder was switched off meanwhile. A project of the queue adds its time to the average per
        project (rough estimate of the queue)."""
        self._flush()
        if self.conn is None or not self.reachable or self.stopped:
            return
        errors = self.conn.execute("SELECT COUNT(*) FROM documents WHERE project = ? AND mail_id IS NULL AND "
                                   "status = 'error'", (self.number,)).fetchone()[0]
        self.conn.execute("UPDATE projects SET done_at = ?, done_profile = ?, done_skipped = ?, pinned = NULL "
                          "WHERE number = ?", (_now(), profile, self.problems + errors, self.number))
        if self.queued:
            zeiten.add(self.conn, zeiten.PROJECT_KEY, time.monotonic() - self.started)
        self.conn.commit()


# -- what the pass does right now --------------------------------------------------------------------
class Live:
    """State of the running pass for the status page (thread-safe; the indexer runs in a thread): phase,
    project, files, and the counts and times of the pass for the estimates."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.running = False
            self.phase = ""
            self.run_started: float | None = None  # time.time() of the start of the pass
            self._run_clear()
            self._clear()

    def _run_clear(self) -> None:
        self.run_seen = 0  # files gone through in the documents pass (unchanged ones included)
        self.run_read = 0  # files read (extracted and written)
        self.run_read_seconds = 0.0
        self.run_projects = 0  # projects started

    def _clear(self) -> None:
        self.project: dict | None = None
        self.seen = 0
        self.total = 0
        self.read = 0
        self.project_started: float | None = None  # time.monotonic() when the project began
        self._pending: list[dict] = []  # projects of the documents pass, in its order; from _at on not started
        self._at = 0
        self.queue_left = 0  # projects of the queue (not done) not started yet
        self.version = -1

    def begin(self) -> None:
        with self._lock:
            self.running, self.phase = True, "startet"
            self.run_started = time.time()
            self._run_clear()
            self._clear()

    def set_phase(self, phase: str) -> None:
        with self._lock:
            if phase.split(" – ")[0] != self.phase.split(" – ")[0]:  # "Bildauswertung – 3 Seite(n) gelesen"
                self._clear()
            self.phase = phase

    def start(self, project: dict, folder: str, pending: list[dict], at: int, plan_version: int,
              queue_left: int = 0, queued: bool = False) -> None:
        with self._lock:
            self.project = {"number": project["number"], "name": project.get("name") or "",
                            "kind": project.get("kind"), "folder": folder, "queued": queued}
            self.seen = self.total = self.read = 0
            self.project_started = time.monotonic()
            self.run_projects += 1
            self._pending, self._at, self.version = pending, at, plan_version
            self.queue_left = queue_left

    def files(self, number: str | None, seen: int, total: int) -> None:
        with self._lock:
            if self.project is not None and self.project["number"] == number:
                self.seen, self.total = seen, total

    def step(self, number: str | None, seen: int, total: int) -> None:
        """One more file gone through (every file: the card shows the real number)."""
        with self._lock:
            self.run_seen += 1
            if self.project is not None and self.project["number"] == number:
                self.seen, self.total = seen, total

    def file_read(self, number: str | None, seconds: float) -> None:
        with self._lock:
            self.run_read += 1
            self.run_read_seconds += seconds
            if self.project is not None and self.project["number"] == number:
                self.read += 1

    def documents_done(self) -> None:
        with self._lock:
            self._clear()

    def vision_project(self, number: str | None, folder: str = "") -> None:
        with self._lock:
            self.project = {"number": number, "name": "", "kind": None, "folder": folder} if number else None
            self.seen = self.total = 0

    def end(self) -> None:
        self.reset()

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {"running": self.running, "phase": self.phase,
                    "project": dict(self.project) if self.project else None, "seen": self.seen, "total": self.total,
                    "read": self.read, "pending": [p["number"] for p in self._pending[self._at:]],
                    "version": self.version, "queue_left": self.queue_left,
                    "run_elapsed": time.time() - self.run_started if self.run_started else None,
                    "project_elapsed": now - self.project_started if self.project_started is not None else None,
                    "run_seen": self.run_seen, "run_read": self.run_read, "run_read_seconds": self.run_read_seconds}


LIVE = Live()


def upcoming(snapshot: dict, current_view: View | None = None, n: int = 3) -> list[str]:
    """The next ``n`` projects of the running documents pass. ``current_view`` (given when the order was
    changed since the pass sorted its projects): in that order – the pass sorts them again before its next
    project; projects of a folder switched off meanwhile are left out."""
    pending = snapshot["pending"]
    if current_view is None or not pending:
        return pending[:n]
    left = set(pending)
    return [e["number"] for e in current_view.order if e["number"] in left and not e["off"]][:n]


# -- Bildauswertung -----------------------------------------------------------------------------------
class VisionOrder:
    """Order of the documents waiting for the model: documents of pinned projects first (pin order), then
    by the folder order; documents without a project (mail attachments not assigned) after the folders. The
    files of projects in folders switched off are left out (mail attachments are not: mails are always read).
    Inside one such step the given order stands (newest first)."""

    def __init__(self, conn: sqlite3.Connection, projects_dir, default: Iterable[str]):
        self.conn, self.projects_dir, self.default = conn, projects_dir, tuple(default)
        self.version = -1
        self._priority: dict[str, tuple] = {}
        self._loose: tuple = (1, 0, 0)
        self.off: set[str] = set()

    def stale(self) -> bool:
        return version(self.conn) != self.version

    def _load(self) -> None:
        self.version = version(self.conn)
        items = entries(self.conn, self.projects_dir, None)
        folder_list = folders(self.conn, {e["folder"] for e in items}, self.default)
        self.off = mark_off(items, folder_list)
        rank = {f["name"]: i for i, f in enumerate(folder_list)}
        enabled = sum(1 for f in folder_list if f["enabled"])
        self._loose = (1, enabled - 0.5, 0)  # after the folders switched on
        self._priority = {e["number"]: (0, e["pinned"], 0) if e["pinned"] is not None
                          else (1, rank.get(e["folder"], len(rank)), 0) for e in items}

    def sort(self, docs: list[tuple]) -> list[int]:
        """``docs``: (document id, project[, mail id]) in the wanted order inside one step. Files of projects
        in folders switched off (not mail attachments) are left out."""
        self._load()
        kept = [d for d in docs if not self.skipped(d[1], d[2] if len(d) > 2 else None, loaded=True)]
        return [d[0] for d in sorted(kept, key=lambda x: self._priority.get(x[1] or "", self._loose))]

    def skipped(self, project: str | None, mail_id: int | None = None, *, loaded: bool = False) -> bool:
        """Is a file of ``project`` left out because its folder is switched off? (Never a mail attachment.)"""
        if not loaded and self.stale():
            self._load()
        return mail_id is None and bool(project) and project in self.off

    def project_of(self, number: str | None) -> str:
        if not number:
            return ""
        row = self.conn.execute("SELECT path, parent, kind FROM projects WHERE number = ?", (number,)).fetchone()
        if row is None:
            return ""
        if row["kind"] != BAUVORHABEN and row["parent"]:
            site = self.conn.execute("SELECT path FROM projects WHERE number = ? AND kind = ?",
                                     (row["parent"], BAUVORHABEN)).fetchone()
            if site is not None:
                return top_folder(site["path"], self.projects_dir)
        return top_folder(row["path"], self.projects_dir)
