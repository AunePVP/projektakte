"""Folder layout of PROJECTS_DIR: where the project folders sit and what their numbers look like.

Projektakte treats every folder whose name starts with a project number as a project (registry.py);
everything inside it is read, whatever its subfolders are called. This module

* analyses a project share read-only and bounded – folder names only, file contents are never
  read – and proposes PROJECT_NUMBER_PATTERN and PROJECT_FOLDER_DEPTH, verified with the scan the
  app itself uses (:func:`registry.scan_projects`). Optionally it counts the files per project
  (which types Projektakte reads and which not) and, given the database, the indexed text per
  project – does a project fit into a language model's context window?
* resolves the *effective* layout: the configured pattern, or with ``PROJECT_NUMBER_PATTERN=auto``
  the result detected once and stored in the ``meta`` table. A stored detection is never replaced
  on its own: a later, different detection is only reported (log, status page) – a half-mounted
  share must never change which folders count as projects – until someone accepts it
  (``python -m projektakte.structure --uebernehmen``).
* is the command line tool ``python -m projektakte.structure`` (German output).

Detection in short: the leading token of every folder name (``BS-2024-0137``, ``2024-017``,
``P12345``, ``2024_017``) is split into a letter prefix and digit groups; names with the same
prefix and number of groups form a candidate scheme (digit counts and separators merged, e.g.
``2024-017`` + ``2024-1234`` → ``\\d{4}-\\d{3,4}``). Every candidate is tried with the app's scan
rules on the listed folder tree; schemes that are too vague (a bare short number, year folders) or
match too few folders, mostly duplicates or only a small share of their level are dropped. Of two
schemes where one sits inside the other's folders, the inner one wins if it makes up most of the
outer folders' contents (customer or year containers), otherwise the outer one (numbered folders
inside projects). Valid schemes on different folders (current projects and an archive in an older
numbering) are joined into one alternation. The listing goes breadth-first, enters only a sample of
the project folders found so far and stops at :data:`MAX_FOLDERS`.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .config import (AUTO, DEFAULT_PROJECT_FOLDER_DEPTH, DEFAULT_PROJECT_NUMBER_PATTERN, PROJECT_FOLDER_DEPTH_MAX,
                     load_settings, pattern_problem)
from .documents import MAX_FILE_BYTES, link_problem
from .extract import SUPPORTED_EXT
from .registry import ProjectFolder, clamp_depth, project_head, projects_root_problem, scan_projects

log = logging.getLogger("projektakte.structure")

MAX_DEPTH = PROJECT_FOLDER_DEPTH_MAX  # levels below PROJECTS_DIR that the analysis looks at
MAX_FOLDERS = 30_000  # folders listed by one analysis (names only)
MIN_PROJECTS = 3  # a scheme needs at least this many project folders
MIN_SHARE = 0.3  # … and at least this share of the folders next to them
MAX_DUPLICATE_SHARE = 0.2  # numbers must be (nearly) unique: a shortened scheme ("BS-2024") repeats
SAMPLE_PROJECTS = 30  # project folders entered to see what lies inside (container or project?)
MAX_CANDIDATES = 12  # candidate schemes tried per round
MAX_GROUPS = 4  # digit groups of a leading token
DEPTH_MIN_SHARE = 0.1  # a level with single stray matches does not raise the proposed depth …
DEPTH_MIN_COUNT = 2  # … unless it holds at least this many projects
MAX_FILES = 200_000  # files counted by the file statistics (all projects together)
MAX_LISTED = 15  # rows of the text report per list
CHARS_PER_TOKEN = 3.5  # German text, typical tokenizer of current models
CONTEXT_WINDOWS = (("32k", 32_000), ("128k", 128_000), ("1M", 1_000_000))
CONTEXT_RESERVE = 4_000  # tokens kept free for instructions, the question and the answer
# a pattern that never matches: the shape of project numbers for search pages while none is known
NO_MATCH = r"(?!)"
# PROJECT_NUMBER_PATTERN=auto: a stored detection is checked again at most this often (and at every
# pass while a different detection is pending) – only to report a difference, never to switch
RECHECK_INTERVAL = timedelta(hours=1)

STORED_KEY = "project_layout_auto"  # meta: JSON of the accepted detection
DIFFERS_KEY = "project_layout_auto_differs"  # meta: JSON of a later detection that differs
CHECKED_KEY = "project_layout_auto_checked"  # meta: time of the last detection (UTC, ISO)

ACCEPT_HINT = "python -m projektakte.structure --uebernehmen"

try:
    from zoneinfo import ZoneInfo

    _LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001 – no tz database: show UTC
    _LOCAL_TZ = timezone.utc


# ------------------------------------------------------------------------------------- formatting
def de_int(n: float | None) -> str:
    """1.234.567 (German thousands separator)."""
    return "–" if n is None else f"{round(n):,}".replace(",", ".")


def de_size(n: int | None) -> str:
    if not n:
        return "0 B"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}".replace(".", ",")
        value /= 1024
    return str(n)


def local_time_text(iso: str | None) -> str:
    """'09.11.2026 11:15' (Europe/Berlin) for a stored UTC time."""
    if not iso:
        return "–"
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(_LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def depth_text(depth: int) -> str:
    """Where project folders are looked for with PROJECT_FOLDER_DEPTH=depth (German)."""
    if depth <= 1:
        return "Projektordner direkt im Projektordner-Verzeichnis"
    if depth == 2:
        return "Projektordner direkt im Verzeichnis oder eine Ebene darunter (z. B. in Jahresordnern)"
    return f"Projektordner direkt im Verzeichnis oder bis zu {depth - 1} Ebenen darunter"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------------------------- folder tree
@dataclass(eq=False)
class _Node:
    """A folder of the listed tree (identity semantics: usable in sets)."""
    name: str
    rel: str  # path below the root, "/"-separated
    depth: int  # 1 = directly below the root
    parent: _Node | None = None
    children: list[_Node] | None = None  # None: not listed
    unreadable: bool = False

    def ancestors(self) -> Iterable[_Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


@dataclass
class _Tree:
    root: _Node
    nodes: list[_Node] = field(default_factory=list)  # all folders except the root, in listing order
    truncated: bool = False
    unreadable: list[str] = field(default_factory=list)

    @property
    def listed(self) -> int:
        return len(self.nodes)


def _subfolders(path: Path, boundary: Path, limit: int) -> tuple[list[str], bool]:
    """Sorted names of the subfolders ``scan_projects`` considers: hidden ones and links whose target
    is missing or outside ``boundary`` are skipped. True if the folder had more than ``limit``
    entries (the rest is not read). Raises OSError if the folder cannot be listed."""
    names: list[str] = []
    cut = False
    with os.scandir(path) as entries:
        for n, entry in enumerate(entries):
            if n >= limit:
                cut = True
                break
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_symlink() and link_problem(Path(entry.path), boundary):
                    continue
                if entry.is_dir():
                    names.append(entry.name)
            except OSError:
                continue
    return sorted(names), cut


def _explore(root_path: Path, max_depth: int, max_folders: int,
             progress: Callable[[str], None] | None) -> _Tree:
    """List the folder tree breadth-first – folder names only. Folders inside a project folder of the
    currently best scheme are not entered; of the project folders themselves only a sample, to see
    whether they are projects or containers of projects. Raises OSError if the root is unreadable."""
    boundary = root_path.resolve()
    tree = _Tree(_Node("", "", 0))

    def expand(node: _Node) -> None:
        path = root_path / node.rel if node.rel else root_path
        try:
            names, cut = _subfolders(path, boundary, max_folders)
        except OSError:
            if node is tree.root:
                raise
            node.unreadable, node.children = True, []
            tree.unreadable.append(node.rel)
            return
        room = max_folders - tree.listed
        if len(names) > room:
            names, cut = names[:room], True
        tree.truncated |= cut
        node.children = [_Node(n, f"{node.rel}/{n}" if node.rel else n, node.depth + 1, node) for n in names]
        tree.nodes.extend(node.children)

    expand(tree.root)
    last_report = time.monotonic()
    while True:
        best = _choose(tree, max_depth).best
        todo = [n for n in tree.nodes if n.children is None and n.depth < max_depth and _worth_listing(n, best)]
        if not todo:
            break
        if tree.listed >= max_folders:
            tree.truncated = True
            break
        level = min(n.depth for n in todo)
        for node in todo:
            if node.depth != level:
                continue
            if tree.listed >= max_folders:
                tree.truncated = True
                break
            expand(node)
            if progress and time.monotonic() - last_report >= 1.0:
                last_report = time.monotonic()
                progress(f"{de_int(tree.listed)} Ordner gelesen (Ebene {level + 1})")
    return tree


def _worth_listing(node: _Node, best: _Candidate | None) -> bool:
    if best is None:
        return True
    if any(a in best.matched for a in node.ancestors()):
        return False  # inside a project folder: its subfolders are its own business
    if node in best.matched:
        return node in best.sample
    return True


# ------------------------------------------------------------------------------------- schemes
_PREFIX = re.compile(r"([A-Za-z]{1,5})([-_ .]?)(?=[0-9])")
_DIGIT_RUN = re.compile(r"[0-9]+")
_SEPARATORS = "-_ ."
_SEP_ORDER = "-_. "
_SEP_ALONE = {"-": "-", "_": "_", ".": r"\.", " ": r"\s"}
_SEP_IN_CLASS = {"-": "-", "_": "_", ".": ".", " ": r"\s"}
_SEP_NAMES = {"-": "Bindestrich", "_": "Unterstrich", ".": "Punkt", " ": "Leerzeichen"}


@dataclass(frozen=True)
class _Token:
    """The leading number of a folder name: ``BS-2024-0137 Schule`` → prefix BS, groups 2024, 0137."""
    prefix: str  # letters before the digits, upper case ("" if none)
    psep: str  # separator between prefix and digits ("" if none)
    groups: tuple[str, ...]
    seps: tuple[str, ...]  # separators between the groups
    glued: bool  # a letter follows the last group directly (no number boundary there)


def _token(name: str) -> _Token | None:
    pos, prefix, psep = 0, "", ""
    if m := _PREFIX.match(name):
        prefix, psep, pos = m.group(1).upper(), m.group(2), m.end()
    groups: list[str] = []
    seps: list[str] = []
    while m := _DIGIT_RUN.match(name, pos):
        groups.append(m.group())
        pos = m.end()
        if (len(groups) < MAX_GROUPS and pos + 1 < len(name) and name[pos] in _SEPARATORS
                and name[pos + 1].isascii() and name[pos + 1].isdigit()):
            seps.append(name[pos])
            pos += 1
            continue
        break
    if not groups:
        return None
    glued = pos < len(name) and name[pos].isascii() and name[pos].isalpha()
    return _Token(prefix, psep, tuple(groups), tuple(seps), glued)


@dataclass(eq=False)
class _Family:
    """Leading tokens with the same prefix and number of digit groups (the first ``k`` groups)."""
    prefix: str
    k: int
    pseps: set[str] = field(default_factory=set)
    seps: list[set[str]] = field(default_factory=list)
    lengths: list[set[int]] = field(default_factory=list)
    years: bool = True  # every first group looks like a year (1900–2099)
    dates: bool = True  # every token is a date (2023-05-12, 12.05.2023) – folders of a day, not projects
    count: int = 0
    longer: int = 0  # members whose number goes on with more digit groups (this scheme would cut it short)
    names: list[str] = field(default_factory=list)

    @property
    def pattern(self) -> str:
        out = self.prefix + (_sep_regex(self.pseps) if self.prefix else "")
        for i, lengths in enumerate(self.lengths):
            out += (_sep_regex(self.seps[i - 1]) if i else "") + _digits_regex(lengths)
        return out

    @property
    def text(self) -> str:
        parts = []
        if self.prefix:
            parts.append(f"„{self.prefix}“")
            if sep := _sep_text(self.pseps):
                parts.append(sep)
        for i, lengths in enumerate(self.lengths):
            if i:
                parts.append(_sep_text(self.seps[i - 1]))
            lo, hi = min(lengths), max(lengths)
            parts.append(f"{lo} Ziffer" if lo == hi == 1 else f"{lo} Ziffern" if lo == hi else f"{lo}–{hi} Ziffern")
        return ", ".join(parts)

    @property
    def specificity(self) -> int:
        return (10 if self.prefix else 0) + self.k

    def vague(self) -> str | None:
        """Why this scheme is too vague to tell projects from other folders – or None."""
        lows = [min(x) for x in self.lengths]
        widths = [max(x) - min(x) for x in self.lengths]
        if any(w > 2 for w in widths):
            return "Ziffernzahl zu uneinheitlich"
        if self.k == 3 and self.dates:
            return "Datumsangaben (Ordner für einen Tag, keine Projektnummern)"
        if self.longer * 2 > self.count:
            return "schneidet längere Nummern ab"
        if not self.prefix and self.k == 1:
            if self.years and self.lengths[0] == {4}:
                return "nur Jahreszahlen (Jahresordner)"
            if lows[0] < 3 or widths[0] > 1 or (widths[0] and lows[0] < 4):
                return "zu unspezifisch (eine kurze oder wechselnd lange Zahl)"
        elif sum(lows) < (3 if 0 < len(self.prefix) < 4 else 4):  # a long prefix is rather a word ("Haus 12")
            return "zu unspezifisch (zu wenige Ziffern)"
        return None


def _sep_regex(chars: set[str]) -> str:
    real = [c for c in _SEP_ORDER if c in chars]
    if not real:
        return ""
    body = _SEP_ALONE[real[0]] if len(real) == 1 else "[" + "".join(_SEP_IN_CLASS[c] for c in real) + "]"
    return body + ("?" if "" in chars else "")


def _sep_text(chars: set[str]) -> str:
    real = [_SEP_NAMES[c] for c in _SEP_ORDER if c in chars]
    text = " oder ".join(real)
    return f"{text} (oder ohne)" if real and "" in chars else text


def _digits_regex(lengths: set[int]) -> str:
    lo, hi = min(lengths), max(lengths)
    return rf"\d{{{lo}}}" if lo == hi else rf"\d{{{lo},{hi}}}"


def _is_date(groups: tuple[str, ...]) -> bool:
    """YYYY-MM-DD or DD-MM-YYYY (any separator)."""
    lens = tuple(len(g) for g in groups)
    if lens == (4, 2, 2):
        year, month, day = groups
    elif lens == (2, 2, 4):
        day, month, year = groups
    else:
        return False
    return 1900 <= int(year) <= 2099 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31


def _families(nodes: Iterable[_Node]) -> dict[tuple[str, int], _Family]:
    out: dict[tuple[str, int], _Family] = {}
    for node in nodes:
        tok = _token(node.name)
        if tok is None:
            continue
        year = len(tok.groups[0]) == 4 and 1900 <= int(tok.groups[0]) <= 2099
        date = len(tok.groups) >= 3 and _is_date(tok.groups[:3])
        for k in range(1, len(tok.groups) + 1):
            if k == len(tok.groups) and tok.glued:
                break  # "2024-017a": no number boundary after the last group
            fam = out.get((tok.prefix, k))
            if fam is None:
                fam = out[(tok.prefix, k)] = _Family(tok.prefix, k, seps=[set() for _ in range(k - 1)],
                                                     lengths=[set() for _ in range(k)])
            fam.count += 1
            fam.longer += k < len(tok.groups)
            fam.pseps.add(tok.psep)
            for i in range(k):
                fam.lengths[i].add(len(tok.groups[i]))
            for i in range(k - 1):
                fam.seps[i].add(tok.seps[i])
            fam.years &= year
            fam.dates &= date
            if len(fam.names) < 5:
                fam.names.append(node.name)
    return out


@dataclass(eq=False)
class _Candidate:
    """A scheme tried on the listed tree with the rules of ``scan_projects``."""
    family: _Family
    pattern: str
    head: re.Pattern
    matches: list[tuple[_Node, str]]  # project folder, number (in scan order)
    duplicates: list[tuple[str, _Node, _Node]]
    exceptions: list[_Node]  # folders without the number next to project folders (Archiv, _Vorlagen …)
    matched: set[_Node]
    sample: set[_Node]
    parts: tuple[str, ...] = ()  # schemes combined into this one (current numbers + an older numbering)

    @property
    def text(self) -> str:
        return self.family.text if not self.parts else " – oder – ".join(self.parts)

    @property
    def depths(self) -> Counter:
        return Counter(node.depth for node, _ in self.matches)

    def problem(self) -> str | None:
        n, dups = len(self.matches), len(self.duplicates)
        if n < MIN_PROJECTS:
            return f"nur {n} passende(r) Ordner"
        if dups > MAX_DUPLICATE_SHARE * (n + dups):
            return f"viele doppelte Nummern ({dups} von {n + dups})"
        if n < MIN_SHARE * (n + len(self.exceptions)):
            return f"nur {round(100 * n / (n + len(self.exceptions)))} % der Ordner auf dieser Ebene"
        return None


def _simulate(root: _Node, family: _Family, pattern: str, max_depth: int) -> _Candidate:
    """What ``scan_projects(root, pattern, max_depth)`` would find in the listed tree."""
    head = project_head(pattern)
    found: dict[str, _Node] = {}
    matches: list[tuple[_Node, str]] = []
    duplicates: list[tuple[str, _Node, _Node]] = []
    others: list[_Node] = []

    def walk(node: _Node, depth: int) -> None:
        for child in node.children or ():
            m = head.match(child.name)
            if m:
                number = m.group("num").upper()
                if number in found:
                    duplicates.append((number, found[number], child))
                else:
                    found[number] = child
                    matches.append((child, number))
            else:
                others.append(child)
                if depth < max_depth:
                    walk(child, depth + 1)

    walk(root, 1)
    bearing = {a for node, _ in matches for a in node.ancestors()}  # folders containing projects
    exceptions = [n for n in others if n not in bearing and n.parent in bearing]
    nodes = [node for node, _ in matches]
    step = max(1, math.ceil(len(nodes) / SAMPLE_PROJECTS))
    return _Candidate(family, pattern, head, matches, duplicates, exceptions, set(nodes), set(nodes[::step]))


def _tighten(root: _Node, cand: _Candidate, max_depth: int) -> _Candidate:
    """The scheme rebuilt from the folders it matched only: names elsewhere (dates inside the sampled
    project folders, "2024-05" next to "2024-017") must not widen it (\\d{2,4} instead of \\d{3,4})."""
    fam = _families(node for node, _ in cand.matches).get((cand.family.prefix, cand.family.k))
    if fam is None or fam.pattern == cand.pattern or fam.vague() or pattern_problem(fam.pattern):
        return cand
    tight = _simulate(root, fam, fam.pattern, max_depth)
    return tight if len(tight.matches) >= len(cand.matches) and not tight.problem() else cand


def _nested(inner: _Candidate, outer: _Candidate) -> bool:
    """Do most project folders of ``inner`` lie inside project folders of ``outer``?"""
    inside = sum(1 for node, _ in inner.matches if any(a in outer.matched for a in node.ancestors()))
    return inside * 2 >= len(inner.matches) > 0


def _container(outer: _Candidate, inner: _Candidate) -> bool:
    """Are the folders of ``outer`` containers of ``inner`` (customer folders with a number, …)?
    Yes if most of their (listed) subfolders are folders of ``inner``; numbered folders inside a
    project (dates, '2023-05 Ortstermin') are only a part of its contents."""
    shares = [sum(1 for c in node.children if inner.head.match(c.name)) / len(node.children)
              for node in outer.matched if node.children]
    return bool(shares) and statistics.median(shares) >= 0.5


@dataclass
class _Choice:
    best: _Candidate | None
    others: list[_Candidate]
    rejected: list[tuple[str, str]]  # (pattern, reason)


def _choose(tree: _Tree, max_depth: int) -> _Choice:
    families = _families(tree.nodes)
    rejected: list[tuple[str, str]] = []
    pool: list[_Family] = []
    for fam in sorted(families.values(), key=lambda f: (-f.count, -f.specificity, f.prefix)):
        if fam.count < MIN_PROJECTS:
            continue
        if why := fam.vague():
            rejected.append((fam.pattern, why))
            continue
        pool.append(fam)
        if len(pool) >= MAX_CANDIDATES:
            break
    valid: list[_Candidate] = []
    for fam in pool:
        pattern = fam.pattern
        if pattern_problem(pattern):  # cannot happen with the generated expressions – be safe
            continue
        cand = _simulate(tree.root, fam, pattern, max_depth)
        if why := cand.problem():
            rejected.append((pattern, why))
        else:
            valid.append(_tighten(tree.root, cand, max_depth))
    alive = list(valid)
    for outer in valid:
        for inner in valid:
            if outer is inner or outer not in alive or inner not in alive or not _nested(inner, outer):
                continue
            if _container(outer, inner):
                alive.remove(outer)
                rejected.append((outer.pattern, "Sammelordner, in denen die Projektordner liegen"))
            else:
                alive.remove(inner)
                rejected.append((inner.pattern, "Unterordner innerhalb der Projektordner"))
    alive.sort(key=lambda c: (-len(c.matches), -c.family.specificity, max(c.depths)))
    if not alive:
        return _Choice(None, [], rejected)
    best = _combined(tree.root, alive, max_depth)
    return _Choice(best, [c for c in alive if c.pattern not in best.parts and c is not best], rejected)


def _combined(root: _Node, alive: list[_Candidate], max_depth: int) -> _Candidate:
    """The best scheme, joined with further valid schemes on other folders – current projects in the
    root and an archive in an older numbering are both projects; choosing one of them would leave the
    other out. Schemes that sit in each other's folders were sorted out before."""
    parts = [alive[0]]
    for other in alive[1:]:
        if len(parts) == 3:
            break
        if not any(other.matched & p.matched for p in parts):
            parts.append(other)
    if len(parts) == 1:
        return alive[0]
    pattern = "(?:" + "|".join(p.pattern for p in parts) + ")"
    combo = _simulate(root, alive[0].family, pattern, max_depth)
    combo.parts = tuple(p.family.text for p in parts)
    if combo.problem() or pattern_problem(pattern) or len(combo.matches) < sum(len(p.matches) for p in parts):
        return alive[0]
    return combo


def _proposed_depth(depths: Counter) -> int:
    """The deepest level that holds a real share of the projects – a single stray match far below
    (a template folder named like a project) does not make every scan look that deep. (The caller
    raises 1 to the default 2 when that finds the same folders.)"""
    total = sum(depths.values())
    levels = [d for d, n in depths.items() if n >= DEPTH_MIN_COUNT or n >= DEPTH_MIN_SHARE * total]
    return max(levels or depths)


# ------------------------------------------------------------------------------------- analysis
@dataclass
class Proposal:
    pattern: str
    depth: int
    description: str  # German: "„BS“, Bindestrich, 4 Ziffern, …"
    projects: int  # found by registry.scan_projects with pattern and depth
    examples: list[dict]  # number, name, path (relative)
    levels: dict[int, int]  # projects per level
    parents: list[list]  # [folder, projects] – containers with the most projects
    exceptions: list[str]  # folders without number next to project folders
    exceptions_total: int
    duplicates: list[dict]  # number, path, duplicate
    deeper: list[str]  # matches below the proposed depth (not found with it)
    second_scheme: dict | None  # exceptions that carry a number scheme of their own
    unreadable: list[str]


@dataclass
class Analysis:
    root: str
    max_depth: int
    problem: str | None = None  # root not readable
    folders: int = 0
    truncated: bool = False
    unreadable: list[str] = field(default_factory=list)
    levels: list[dict] = field(default_factory=list)
    proposal: Proposal | None = None
    reason: str | None = None  # why no scheme was proposed
    alternatives: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    seconds: float = 0.0
    projects: list[ProjectFolder] = field(default_factory=list, repr=False)

    @property
    def exit_code(self) -> int:
        return 2 if self.problem else 0 if self.proposal else 1

    def to_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data.pop("projects")
        return data


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _level_summary(tree: _Tree) -> list[dict]:
    out = []
    for depth in sorted({n.depth for n in tree.nodes}):
        nodes = [n for n in tree.nodes if n.depth == depth]
        out.append({"level": depth, "folders": len(nodes),
                    "with_number": sum(1 for n in nodes if _token(n.name)),
                    "examples": [n.name for n in nodes[:6]]})
    return out


def _no_scheme_reason(tree: _Tree, choice: _Choice) -> str:
    if not tree.nodes:
        return "Der Ordner enthält keine Unterordner – ist die Freigabe eingebunden?"
    if not any(_token(n.name) for n in tree.nodes):
        return "Kein Ordnername beginnt mit einer Nummer."
    if choice.rejected:
        reasons = "; ".join(f"{p}: {why}" for p, why in choice.rejected[:3])
        return f"Kein Nummernschema ist eindeutig genug ({reasons})."
    return (f"Zu wenige Ordner mit gleich aufgebauter Nummer am Anfang (mindestens {MIN_PROJECTS} nötig, "
            "z. B. 2024-017 Kita, 2024-018 Schule …).")


def _second_scheme(exceptions: list[_Node], pattern: str) -> dict | None:
    """Exceptions that start with a number scheme of their own (projects in an older numbering?)."""
    families = [f for f in _families(exceptions).values() if f.count >= 2 and not f.vague()]
    if not families:
        return None
    fam = max(families, key=lambda f: (f.count, f.specificity))
    return {"pattern": fam.pattern, "folders": fam.count, "examples": fam.names[:3],
            "combined": f"(?:{pattern}|{fam.pattern})"}


def analyse(root: Path | str, *, max_depth: int = MAX_DEPTH, max_folders: int = MAX_FOLDERS,
            progress: Callable[[str], None] | None = None) -> Analysis:
    """Analyse the folder layout below ``root`` (read-only, folder names only) and propose a
    project number scheme and folder depth, verified with :func:`registry.scan_projects`."""
    started = time.monotonic()
    root = Path(root)
    max_depth = clamp_depth(max_depth)
    result = Analysis(root=str(root), max_depth=max_depth)
    if problem := projects_root_problem(root):
        result.problem = problem
        return result
    try:
        tree = _explore(root, max_depth, max(1, max_folders), progress)
    except OSError as exc:
        result.problem = f"Projektordner {root} nicht lesbar ({exc.strerror or exc})"
        return result
    result.folders, result.truncated = tree.listed, tree.truncated
    result.unreadable = tree.unreadable[:MAX_LISTED * 4]
    result.levels = _level_summary(tree)
    choice = _choose(tree, max_depth)
    result.rejected = [{"pattern": p, "reason": why} for p, why in choice.rejected[:8]]
    best = choice.best
    if best is None:
        result.reason = _no_scheme_reason(tree, choice)
        result.seconds = round(time.monotonic() - started, 2)
        return result
    depth = _proposed_depth(best.depths)
    description = best.text
    if depth < DEFAULT_PROJECT_FOLDER_DEPTH and max_depth >= DEFAULT_PROJECT_FOLDER_DEPTH:
        # the default finds the very same folders today and also projects put into a year folder later
        wider = _simulate(tree.root, best.family, best.pattern, DEFAULT_PROJECT_FOLDER_DEPTH)
        if {n for _, n in wider.matches} == {n for _, n in _simulate(tree.root, best.family, best.pattern,
                                                                         depth).matches}:
            depth = DEFAULT_PROJECT_FOLDER_DEPTH
    final = _simulate(tree.root, best.family, best.pattern, depth)
    unreadable: list[Path] = []
    duplicates: list[tuple[str, Path, Path]] = []
    found = scan_projects(root, best.pattern, depth, unreadable=unreadable, duplicates=duplicates)
    if len(found) < MIN_PROJECTS:  # the share changed while it was read
        result.reason = (f"Das Schema {best.pattern} fand beim Gegenprüfen nur {len(found)} Projektordner – "
                         "Freigabe vollständig eingebunden?")
        result.seconds = round(time.monotonic() - started, 2)
        return result
    parents = Counter(_rel(p.path.parent, root) for p in found if p.path.parent != root)
    result.projects = found
    result.proposal = Proposal(
        pattern=best.pattern, depth=depth, description=description, projects=len(found),
        examples=[{"number": p.number, "name": p.name, "path": _rel(p.path, root)} for p in found[:MAX_LISTED]],
        levels=dict(sorted(Counter(len(_rel(p.path, root).split("/")) for p in found).items())),
        parents=[[k, v] for k, v in parents.most_common(8)],
        exceptions=[n.rel for n in final.exceptions[:MAX_LISTED * 2]], exceptions_total=len(final.exceptions),
        duplicates=[{"number": n, "path": _rel(a, root), "duplicate": _rel(b, root)}
                    for n, a, b in duplicates[:MAX_LISTED]],
        deeper=[node.rel for node, _ in best.matches if node.depth > depth][:MAX_LISTED],
        second_scheme=_second_scheme(final.exceptions, best.pattern),
        unreadable=[_rel(p, root) for p in unreadable[:MAX_LISTED]])
    result.alternatives = [{"pattern": c.pattern, "description": c.text, "projects": len(c.matches),
                            "examples": [n.name for n, _ in c.matches[:3]]} for c in choice.others[:3]]
    result.seconds = round(time.monotonic() - started, 2)
    return result


# ------------------------------------------------------------------------------------- files
_IGNORED_FILES = ("thumbs.db", "desktop.ini")


def file_statistics(projects: list[ProjectFolder], root: Path | str, *, max_files: int = MAX_FILES,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Files per project folder – count, size, file types – as the indexer sees them (hidden files,
    Office lock files and links leaving ``root`` skipped, linked folders not entered). Only names
    and sizes are read, never contents; stops after ``max_files`` files."""
    boundary = Path(root).resolve()
    by_ext: dict[str, list[int]] = {}
    per_project: list[dict] = []
    total = unreadable = 0
    truncated = False
    last_report = time.monotonic()
    for project in projects:
        row = {"number": project.number, "files": 0, "bytes": 0, "readable": 0, "too_big": 0}
        stack = [project.path]
        while stack and not truncated:
            folder = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                unreadable += 1
                continue
            for entry in entries:
                name = entry.name
                if name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                        continue
                    if entry.is_symlink():
                        if link_problem(Path(entry.path), boundary) or not entry.is_file():
                            continue
                    elif not entry.is_file(follow_symlinks=False):
                        continue
                    if name.startswith("~$") or name.lower() in _IGNORED_FILES:
                        continue
                    size = entry.stat().st_size
                except OSError:
                    continue
                ext = Path(name).suffix.lower() or "(ohne Endung)"
                slot = by_ext.setdefault(ext, [0, 0])
                slot[0] += 1
                slot[1] += size
                row["files"] += 1
                row["bytes"] += size
                if ext in SUPPORTED_EXT:
                    if size <= MAX_FILE_BYTES:
                        row["readable"] += 1
                    else:
                        row["too_big"] += 1
                total += 1
                if total >= max_files:
                    truncated = True
                    break
                if progress and time.monotonic() - last_report >= 1.0:
                    last_report = time.monotonic()
                    progress(f"{de_int(total)} Dateien gezählt ({project.number})")
        per_project.append(row)
        if truncated:
            break
    extensions = [{"ext": ext, "files": n, "bytes": size, "read": ext in SUPPORTED_EXT}
                  for ext, (n, size) in sorted(by_ext.items(), key=lambda kv: (-kv[1][0], kv[0]))]
    readable = sum(r["readable"] for r in per_project)
    return {"projects": len(per_project), "files": total, "bytes": sum(r["bytes"] for r in per_project),
            "readable": readable, "too_big": sum(r["too_big"] for r in per_project),
            "not_readable": total - readable, "extensions": extensions,
            "unsupported": [e for e in extensions if not e["read"]][:10],
            "per_project": per_project, "truncated": truncated, "max_files": max_files,
            "unreadable_folders": unreadable}


# ------------------------------------------------------------------------------------- context size
def tokens_for(chars: int) -> int:
    """Estimated tokens of German text (≈ CHARS_PER_TOKEN characters per token)."""
    return math.ceil(chars / CHARS_PER_TOKEN)


def fits(tokens: int) -> dict[str, bool]:
    """Does a text of ``tokens`` fit into the context windows (with room for question and answer)?"""
    return {label: tokens + CONTEXT_RESERVE <= size for label, size in CONTEXT_WINDOWS}


def context_statistics(db_path: Path | str) -> dict | None:
    """Indexed text per active project from the database (opened read-only): characters of the
    project files and of the mails assigned to it (with their attachments) – the overlap that chunking
    repeats between sections counted once –, estimated tokens and whether that fits into a 32k, 128k
    or 1M context window. None without a database."""
    from .webtext import trim_overlap

    path = Path(db_path)
    if not path.is_file():
        return None
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            projects = {r["number"]: {"number": r["number"], "name": r["name"], "files": 0, "documents": 0,
                                      "mails": 0, "chars_documents": 0, "chars_mails": 0}
                        for r in conn.execute("SELECT number, name FROM projects WHERE active = 1")}
            for r in conn.execute("SELECT project, COUNT(*) AS n FROM documents WHERE mail_id IS NULL "
                                  "AND project IS NOT NULL GROUP BY project"):
                if r["project"] in projects:
                    projects[r["project"]]["files"] = r["n"]
            for r in conn.execute("SELECT project, COUNT(*) AS n FROM mails WHERE deleted = 0 "
                                  "AND project IS NOT NULL GROUP BY project"):
                if r["project"] in projects:
                    projects[r["project"]]["mails"] = r["n"]
            rows = conn.execute(
                "SELECT project, doc_id, mail_id, page, heading, text FROM chunks WHERE project IN "
                "(SELECT number FROM projects WHERE active = 1) ORDER BY project, doc_id, mail_id, ord")
            doc_tokens: list[int] = []
            mail_chars: dict[int, int] = {}
            prev = None
            group_chars = 0

            def close_group(row) -> None:
                p = projects[row["project"]]
                if row["mail_id"] is None:
                    p["chars_documents"] += group_chars
                    p["documents"] += 1
                    doc_tokens.append(tokens_for(group_chars))
                else:
                    p["chars_mails"] += group_chars
                    mail_chars[row["mail_id"]] = mail_chars.get(row["mail_id"], 0) + group_chars

            for row in rows:
                same = prev is not None and (prev["project"], prev["doc_id"], prev["mail_id"]) == (
                    row["project"], row["doc_id"], row["mail_id"])
                if prev is not None:
                    text = prev["text"] or ""
                    if same and (prev["page"], prev["heading"] or "") == (row["page"], row["heading"] or ""):
                        text = trim_overlap(text, row["text"] or "")
                    group_chars += len(text)
                    if not same:
                        close_group(prev)
                        group_chars = 0
                prev = row
            if prev is not None:
                group_chars += len(prev["text"] or "")
                close_group(prev)
        except sqlite3.Error as exc:
            return {"error": f"Datenbank nicht lesbar ({exc})", "projects": []}
    out = []
    for p in projects.values():
        p["chars"] = p["chars_documents"] + p["chars_mails"]
        p["tokens"] = tokens_for(p["chars"])
        p["fits"] = fits(p["tokens"])
        out.append(p)
    out.sort(key=lambda p: (-p["tokens"], p["number"]))
    mean_doc = statistics.fmean(doc_tokens) if doc_tokens else 0.0
    mean_mail = statistics.fmean(tokens_for(c) for c in mail_chars.values()) if mail_chars else 0.0
    summary = {
        "projects": len(out),
        "fit": {label: sum(1 for p in out if p["fits"][label]) for label, _ in CONTEXT_WINDOWS},
        "largest": {"number": out[0]["number"], "tokens": out[0]["tokens"]} if out else None,
        "median_project_tokens": round(statistics.median([p["tokens"] for p in out])) if out else 0,
        "documents_with_text": len(doc_tokens),
        "mean_document_tokens": round(mean_doc),
        "median_document_tokens": round(statistics.median(doc_tokens)) if doc_tokens else 0,
        "mean_mail_tokens": round(mean_mail),
        # documents of average size that fit into each window – "ab wie vielen Dateien passt es nicht mehr"
        "documents_per_window": {label: int((size - CONTEXT_RESERVE) // mean_doc) if mean_doc else None
                                 for label, size in CONTEXT_WINDOWS},
    }
    return {"database": str(path), "chars_per_token": CHARS_PER_TOKEN, "reserve_tokens": CONTEXT_RESERVE,
            "windows": dict(CONTEXT_WINDOWS), "summary": summary, "projects": out}


# ------------------------------------------------------------------------------------- effective layout
@dataclass(frozen=True)
class Layout:
    """Which folders count as projects: the effective PROJECT_NUMBER_PATTERN and folder depth."""
    pattern: str | None  # None: no usable scheme – the project sync is skipped
    depth: int
    auto: bool  # PROJECT_NUMBER_PATTERN=auto
    depth_auto: bool  # the depth comes from the detection (PROJECT_FOLDER_DEPTH unset or auto)
    detected_at: str | None = None
    examples: tuple[str, ...] = ()
    projects: int | None = None  # project folders found when it was detected
    problem: str | None = None  # German: why there is no usable scheme
    differs: dict | None = None  # a later detection that differs (the stored one stays in use)

    @property
    def source(self) -> str:
        if not self.auto:
            return "eingestellt"
        if self.pattern:
            return f"automatisch erkannt am {local_time_text(self.detected_at)}"
        return "automatisch – noch nicht erkannt"

    @property
    def warning(self) -> str | None:
        d = self.differs
        if not d or not self.pattern:
            return None
        examples = ", ".join(d.get("examples") or [])
        stored = d.get("stored_projects")
        return (f"Eine neuere Erkennung ({local_time_text(d.get('detected_at'))}) ergibt ein anderes Schema: "
                f"{d.get('pattern')}, Tiefe {d.get('depth')} ({d.get('projects')} Projektordner"
                f"{', z. B. ' + examples if examples else ''}). Weiter verwendet wird das gespeicherte "
                f"{self.pattern}, Tiefe {self.depth}"
                f"{f' (findet derzeit {stored} Projektordner)' if stored is not None else ''}. "
                f"Übernehmen mit: {ACCEPT_HINT}")

    def summary(self) -> str:
        """One German line for the start-up log."""
        if self.pattern is None:
            return ("automatisch – noch nicht erkannt (beim nächsten Indexierungslauf)" if self.auto
                    else "unbrauchbar – Projekte werden nicht abgeglichen")
        depth = f"Tiefe {self.depth}" + (" (automatisch)" if self.auto and self.depth_auto else "")
        return f"{self.pattern} ({self.source}), {depth}"

    def to_dict(self) -> dict:
        return {"pattern": self.pattern, "depth": self.depth, "auto": self.auto, "depth_auto": self.depth_auto,
                "source": self.source, "depth_text": depth_text(self.depth), "detected_at": self.detected_at,
                "examples": list(self.examples), "projects_at_detection": self.projects,
                "problem": self.problem, "warning": self.warning,
                "differs": {k: v for k, v in self.differs.items() if k != "root"} if self.differs else None}


def root_key(path: Path | str) -> str:
    """PROJECTS_DIR as stored with a detection (absolute)."""
    return os.path.abspath(os.fspath(path))


def _load(conn: sqlite3.Connection | None, key: str) -> dict | None:
    if conn is None:
        return None
    try:
        raw = db.get_meta(conn, key)
    except sqlite3.Error:
        return None
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _valid_record(rec: dict | None) -> bool:
    return (isinstance(rec, dict) and isinstance(rec.get("pattern"), str) and not pattern_problem(rec["pattern"])
            and isinstance(rec.get("depth"), int) and 1 <= rec["depth"] <= MAX_DEPTH)


def stored_detection(conn: sqlite3.Connection | None, projects_dir: Path | str) -> dict | None:
    """The accepted automatic detection for this PROJECTS_DIR – None if there is none, it is unusable
    or it was made for another folder (e.g. the demo data set before the switch to real data)."""
    rec = _load(conn, STORED_KEY)
    if not _valid_record(rec) or rec.get("root") != root_key(projects_dir):
        return None
    return rec


def _record(proposal: Proposal, root: Path | str, when: datetime) -> dict:
    return {"pattern": proposal.pattern, "depth": proposal.depth, "detected_at": _iso(when),
            "projects": proposal.projects,
            "examples": [f"{e['number']} {e['name']}".strip() for e in proposal.examples[:3]],
            "root": root_key(root)}


def store_detection(conn: sqlite3.Connection, proposal: Proposal, root: Path | str,
                    now: datetime | None = None) -> dict:
    """Accept a detection: it is used from now on (PROJECT_NUMBER_PATTERN=auto) and a pending
    different detection is forgotten. The caller commits."""
    when = now or _now()
    rec = _record(proposal, root, when)
    db.set_meta(conn, STORED_KEY, json.dumps(rec, ensure_ascii=False))
    db.set_meta(conn, CHECKED_KEY, _iso(when))
    conn.execute("DELETE FROM meta WHERE key = ?", (DIFFERS_KEY,))
    return rec


def _open_readonly(settings) -> sqlite3.Connection | None:
    path = getattr(settings, "db_path", None)
    if not path or not Path(path).is_file():
        return None
    try:
        return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def current_layout(settings, conn: sqlite3.Connection | None = None) -> Layout:
    """The effective layout without detecting anything: the configured pattern, or the stored
    automatic detection (read from ``conn`` or, without one, the database opened read-only)."""
    raw = getattr(settings, "project_number_pattern", None) or DEFAULT_PROJECT_NUMBER_PATTERN
    configured_depth = getattr(settings, "project_folder_depth", None)
    if raw != AUTO:
        problem = pattern_problem(raw)
        return Layout(None if problem else raw, clamp_depth(configured_depth), False, False,
                      problem=f"PROJECT_NUMBER_PATTERN={raw} ist unbrauchbar: {problem}" if problem else None)
    if conn is None and (own := _open_readonly(settings)) is not None:
        with closing(own):
            return current_layout(settings, own)
    rec = stored_detection(conn, settings.projects_dir)
    if rec is None:
        return Layout(None, clamp_depth(configured_depth), True, configured_depth is None,
                      problem="Projektnummern-Schema noch nicht erkannt (PROJECT_NUMBER_PATTERN=auto) – "
                              "das geschieht beim nächsten Indexierungslauf")
    differs = _load(conn, DIFFERS_KEY)
    return Layout(rec["pattern"], configured_depth or rec["depth"], True, configured_depth is None,
                  detected_at=rec.get("detected_at"), examples=tuple(rec.get("examples") or ()),
                  projects=rec.get("projects"), differs=differs if _valid_record(differs) else None)


def number_pattern(settings, conn: sqlite3.Connection | None = None) -> str:
    """The effective shape of project numbers for finding them in texts (mails, search queries);
    :data:`NO_MATCH` while there is none (not detected yet, unusable setting)."""
    return current_layout(settings, conn).pattern or NO_MATCH


def _parse_time(value: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def layout_for_index(conn: sqlite3.Connection, settings, *, now: datetime | None = None,
                     max_folders: int = MAX_FOLDERS) -> tuple[Layout, dict]:
    """The layout for an indexing pass. With PROJECT_NUMBER_PATTERN=auto this detects the scheme
    when nothing is stored yet and stores it; a stored one is checked again now and then, and a
    different result is only reported – the stored scheme stays in use until it is accepted
    (stability: a half-mounted share must never change which folders count as projects). Returns
    the layout and a small summary for the run stats (no warning there – the status page reads it)."""
    layout = current_layout(settings, conn)
    if not layout.auto:
        return layout, {}
    now = now or _now()
    root = Path(settings.projects_dir)
    info = {"pattern": layout.pattern, "depth": layout.depth, "source": layout.source}
    if problem := projects_root_problem(root):
        return (layout if layout.pattern else dataclasses.replace(layout, problem=problem)), info
    rec = stored_detection(conn, root)
    if rec is not None and layout.differs is None:
        checked = _parse_time(db.get_meta(conn, CHECKED_KEY))
        if checked is not None and timedelta(0) <= now - checked < RECHECK_INTERVAL:
            return layout, info
    try:
        result = analyse(root, max_folders=max_folders)
    except Exception as exc:  # noqa: BLE001 – a detection problem must never stop the indexing
        log.exception("Projektnummern-Schema: Erkennung fehlgeschlagen")
        if rec is not None:
            return layout, info
        return dataclasses.replace(layout, problem=f"Erkennung des Projektnummern-Schemas fehlgeschlagen "
                                                   f"({type(exc).__name__}: {exc})"), info
    db.set_meta(conn, CHECKED_KEY, _iso(now))
    proposal = result.proposal
    if rec is None:
        if proposal is None:
            conn.commit()
            problem = (f"Kein eindeutiges Projektnummern-Schema in {root} erkannt: {result.reason or result.problem} "
                       "Bitte PROJECT_NUMBER_PATTERN einstellen (Vorschlag und Ordnerübersicht: "
                       "python -m projektakte.structure)")
            return dataclasses.replace(layout, problem=problem), {**info, "detected": False}
        store_detection(conn, proposal, root, now)
        conn.commit()
        log.info("Projektnummern-Schema automatisch erkannt und gespeichert: %s, Tiefe %d (%d Projektordner, "
                 "z. B. %s)", proposal.pattern, proposal.depth, proposal.projects,
                 ", ".join(e["path"] for e in proposal.examples[:3]))
        layout = current_layout(settings, conn)
        return layout, {"pattern": layout.pattern, "depth": layout.depth, "source": layout.source,
                        "detected": True}
    same = proposal is not None and proposal.pattern == rec["pattern"] and (
        not layout.depth_auto or proposal.depth == rec["depth"])
    if proposal is None:
        pass  # nothing clear (share half mounted, emptied?) – keep everything as it is
    elif same:
        if layout.differs is not None:
            conn.execute("DELETE FROM meta WHERE key = ?", (DIFFERS_KEY,))
            log.info("Projektnummern-Schema: die neue Erkennung stimmt wieder mit dem gespeicherten %s überein",
                     rec["pattern"])
    else:
        stored_found = len(scan_projects(root, rec["pattern"], layout.depth, unreadable=[], duplicates=[]))
        new = {**_record(proposal, root, now), "stored_projects": stored_found}
        old = layout.differs or {}
        if (old.get("pattern"), old.get("depth")) != (new["pattern"], new["depth"]):
            log.warning("Projektnummern-Schema: eine neue Erkennung ergibt %s, Tiefe %d (%d Projektordner) – "
                        "weiter verwendet wird das gespeicherte %s, Tiefe %d (%d Projektordner). Übernehmen mit: %s",
                        new["pattern"], new["depth"], new["projects"], rec["pattern"], layout.depth,
                        stored_found, ACCEPT_HINT)
        db.set_meta(conn, DIFFERS_KEY, json.dumps(new, ensure_ascii=False))
    conn.commit()
    layout = current_layout(settings, conn)
    if layout.differs:
        info["differs"] = layout.differs["pattern"]
    return layout, info


# ------------------------------------------------------------------------------------- command line
def current_check(root: Path, settings) -> dict:
    """What the current settings (environment, stored detection) find in ``root``."""
    layout = current_layout(settings)
    depth_setting = getattr(settings, "project_folder_depth", None)
    out = {"setting": settings.project_number_pattern,
           "depth_setting": AUTO if depth_setting is None else depth_setting,
           "pattern": layout.pattern, "depth": layout.depth, "source": layout.source, "problem": layout.problem,
           "projects": None, "examples": []}
    if layout.pattern:
        found = scan_projects(root, layout.pattern, layout.depth, unreadable=[], duplicates=[])
        out["projects"] = len(found)
        out["examples"] = [_rel(p.path, root) for p in found[:5]]
    return out


def env_lines(proposal: Proposal | None) -> list[str]:
    if proposal is None:
        return []
    return [f"PROJECT_NUMBER_PATTERN={proposal.pattern}", f"PROJECT_FOLDER_DEPTH={proposal.depth}"]


def _table(rows: list[list[str]], right: Iterable[int] = ()) -> list[str]:
    right = set(right)
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  " + "  ".join(c.rjust(widths[i]) if i in right else c.ljust(widths[i])
                             for i, c in enumerate(r)).rstrip() for r in rows]


def format_report(analysis: Analysis, current: dict | None = None, files: dict | None = None,
                  context: dict | None = None) -> str:
    """The German text report of ``python -m projektakte.structure``."""
    a, p = analysis, analysis.proposal
    out = [f"Projektordner: {a.root}"]
    if a.problem:
        return "\n".join(out + [f"FEHLER: {a.problem}"])
    out.append(f"Gelesen: {de_int(a.folders)} Ordner (nur Namen, bis Ebene {a.max_depth}) in "
               f"{str(a.seconds).replace('.', ',')} s" + (" – Liste gekürzt" if a.truncated else ""))
    if a.unreadable:
        out.append(f"Nicht lesbar: {', '.join(a.unreadable[:5])}" + (" …" if len(a.unreadable) > 5 else ""))
    out.append("")
    if p is None:
        out.append("Kein eindeutiges Projektnummern-Schema gefunden.")
        out.append(f"  {a.reason}")
        out.append("")
        out.append("Ordner je Ebene:")
        for lv in a.levels[:4]:
            out.append(f"  Ebene {lv['level']}: {de_int(lv['folders'])} Ordner, {de_int(lv['with_number'])} mit Nummer "
                       f"am Anfang (z. B. {', '.join(lv['examples'][:4])})")
        out.append("")
        out.append("PROJECT_NUMBER_PATTERN bitte von Hand einstellen (regulärer Ausdruck für die Nummer am Anfang "
                   "der Projektordner, z. B. \\d{4}-\\d{3} für „2024-017 Kita“) und mit diesem Befehl prüfen.")
    else:
        out.append("Erkanntes Schema")
        out.append(f"  Nummer:    {p.description}")
        out.append(f"  Ausdruck:  {p.pattern}")
        levels = ", ".join(f"Ebene {lv}: {de_int(n)}" for lv, n in p.levels.items())
        out.append(f"  Tiefe:     {p.depth} – {depth_text(p.depth)}")
        out.append(f"  Projekte:  {de_int(p.projects)} gefunden (Prüfung mit dem Scan der App; {levels})")
        if p.parents:
            out.append("  liegen in: " + ", ".join(f"{name} ({de_int(n)})" for name, n in p.parents[:4])
                       + (" …" if len(p.parents) > 4 else ""))
        out.append("")
        out.extend(_table([[e["number"], e["name"][:40], e["path"]] for e in p.examples[:10]]))
        if p.projects > 10:
            out.append(f"  … und {de_int(p.projects - 10)} weitere")
        out.append("")
        if p.exceptions_total:
            out.append(f"Ausnahmen – Ordner ohne passende Nummer neben den Projektordnern "
                       f"({de_int(p.exceptions_total)}), werden nicht gelesen:")
            more = " …" if p.exceptions_total > MAX_LISTED else ""
            out.append("  " + " · ".join(p.exceptions[:MAX_LISTED]) + more)
        else:
            out.append("Ausnahmen: keine – alle Ordner neben den Projektordnern haben eine passende Nummer.")
        if p.second_scheme:
            s = p.second_scheme
            out.append(f"  Davon beginnen {s['folders']} selbst mit einer Nummer ({s['pattern']}, z. B. "
                       f"{', '.join(s['examples'])}). Falls das auch Projekte sind: "
                       f"PROJECT_NUMBER_PATTERN={s['combined']}")
        if p.duplicates:
            out.append(f"Doppelte Nummern ({len(p.duplicates)}) – gelesen wird nur der erste Ordner:")
            out.extend(f"  {d['number']}: {d['path']}  und  {d['duplicate']}" for d in p.duplicates[:MAX_LISTED])
        if p.deeper:
            out.append(f"Einzelne passende Ordner tiefer als Ebene {p.depth} (werden mit dieser Tiefe nicht erfasst): "
                       + " · ".join(p.deeper[:5]))
        if p.unreadable:
            out.append("Nicht lesbare Ordner beim Scan: " + ", ".join(p.unreadable[:5]))
        for alt in a.alternatives:
            out.append(f"Weiteres mögliches Schema: {alt['pattern']} ({de_int(alt['projects'])} Ordner, z. B. "
                       f"{', '.join(alt['examples'])})")
    if current is not None:
        out.append("")
        depth = current["depth_setting"]
        out.append(f"Aktuelle Einstellung: PROJECT_NUMBER_PATTERN={current['setting']}, PROJECT_FOLDER_DEPTH={depth}")
        if current["pattern"] is None:
            out.append(f"  {current['problem']}")
        else:
            if current["setting"] == AUTO:
                out.append(f"  verwendet: {current['pattern']}, Tiefe {current['depth']} ({current['source']})")
            n = current["projects"]
            verdict = ("wie der Vorschlag" if p and n == p.projects and current["pattern"] == p.pattern
                       else f"Vorschlag: {de_int(p.projects)}" if p else "")
            out.append(f"  findet {de_int(n)} Projektordner" + (f" ({verdict})" if verdict else "")
                       + (" – passt nicht zu diesem Ordner" if n == 0 else ""))
    if files is not None:
        out.append("")
        out.extend(_format_files(files))
    if context is not None:
        out.append("")
        out.extend(_format_context(context))
    if p is not None:
        out.append("")
        out.append("In projektakte.env eintragen:")
        out.extend(f"  {line}" for line in env_lines(p))
        out.append("(oder PROJECT_NUMBER_PATTERN=auto: Projektakte erkennt das Schema beim ersten Lauf selbst und "
                   "behält es, bis es mit --uebernehmen bestätigt neu erkannt wird)")
    return "\n".join(out)


def _format_files(f: dict) -> list[str]:
    out = [f"Dateien in {de_int(f['projects'])} Projektordnern: {de_int(f['files'])} ({de_size(f['bytes'])})"
           + (f" – nach {de_int(f['max_files'])} Dateien abgebrochen" if f["truncated"] else "")]
    read = [e for e in f["extensions"] if e["read"]]
    out.append(f"  liest Projektakte:       {de_int(f['readable'])}"
               + (" – " + ", ".join(f"{e['ext']} {de_int(e['files'])}" for e in read[:8]) if read else ""))
    if f["too_big"]:
        out.append(f"  davon zu groß (über {de_size(MAX_FILE_BYTES)}): {de_int(f['too_big'])}")
    out.append(f"  liest Projektakte nicht: {de_int(f['not_readable'])}")
    if f["unsupported"]:
        out.extend(_table([[e["ext"], de_int(e["files"]), de_size(e["bytes"])] for e in f["unsupported"]],
                          right=(1, 2)))
    out.append("  (Bilder und Scans nur mit Bildmodell, LLM_VISION=1)")
    if f["unreadable_folders"]:
        out.append(f"  nicht lesbare Unterordner: {de_int(f['unreadable_folders'])}")
    biggest = sorted((r for r in f["per_project"] if r["files"]), key=lambda r: -r["files"])[:5]
    if biggest:
        out.append("  meiste Dateien: " + ", ".join(f"{r['number']} ({de_int(r['files'])})" for r in biggest))
    return out


def _format_context(c: dict) -> list[str]:
    if c.get("missing"):
        return [(f"Textmenge im Index: keine Datenbank unter {c['missing']} (gibt es erst nach dem ersten "
                 "Indexierungslauf; anderes Datenverzeichnis mit --data-dir)")]
    if c.get("error"):
        return [f"Textmenge im Index: {c['error']}"]
    s = c["summary"]
    out = [(f"Textmenge im Index ({c['database']}; ≈ {str(CHARS_PER_TOKEN).replace('.', ',')} Zeichen je Token, "
            f"{de_int(CONTEXT_RESERVE)} Token Reserve für Frage und Antwort)")]
    if not c["projects"]:
        return out + ["  noch keine Projekte im Index"]
    labels = [label for label, _ in CONTEXT_WINDOWS]
    rows = [["Projekt", "Dateien", "Mails", "Zeichen", "Token", *labels]]
    for p in c["projects"][:MAX_LISTED]:
        rows.append([p["number"], de_int(p["documents"]), de_int(p["mails"]), de_int(p["chars"]), de_int(p["tokens"]),
                     *("ja" if p["fits"][label] else "–" for label in labels)])
    out.extend(_table(rows, right=range(1, 5 + len(labels))))
    if len(c["projects"]) > MAX_LISTED:
        out.append(f"  … und {de_int(len(c['projects']) - MAX_LISTED)} kleinere Projekte")
    out.append("  passen ganz hinein: " + ", ".join(f"{label}: {de_int(s['fit'][label])} von {de_int(s['projects'])}"
                                                  for label in labels))
    if s["mean_document_tokens"]:
        per = s["documents_per_window"]
        out.append(f"  ein Dokument hat im Mittel {de_int(s['mean_document_tokens'])} Token (Median "
                   f"{de_int(s['median_document_tokens'])}) – es passen also etwa "
                   + ", ".join(f"{de_int(per[label])} Dokumente in {label}" for label in labels))
    if s["mean_mail_tokens"]:
        out.append(f"  eine Mail (mit Anhängen) hat im Mittel {de_int(s['mean_mail_tokens'])} Token")
    out.append("  Für Antworten schickt Projektakte nie ein ganzes Projekt, sondern nur die passendsten Abschnitte "
               "(ASK_CONTEXT_CHARS) – die Projektgröße begrenzt die Antworten nicht.")
    return out


def _progress_printer(enabled: bool) -> Callable[[str], None] | None:
    if not enabled:
        return None
    tty = sys.stderr.isatty()
    state = {"last": 0.0}

    def show(text: str) -> None:
        now = time.monotonic()
        if not tty and now - state["last"] < 5.0:
            return
        state["last"] = now
        sys.stderr.write(f"\r… {text}\033[K" if tty else f"… {text}\n")
        sys.stderr.flush()
    return show


def _end_progress(enabled: bool) -> None:
    if enabled and sys.stderr.isatty():
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m projektakte.structure",
        description="Ordnerstruktur des Projektordners prüfen: erkennt, wie Projektnummern aufgebaut sind und auf "
                    "welcher Ebene die Projektordner liegen, und schlägt PROJECT_NUMBER_PATTERN und "
                    "PROJECT_FOLDER_DEPTH vor. Liest nur Ordner- und Dateinamen, nie Inhalte, und schreibt nichts "
                    "(außer mit --uebernehmen).",
        epilog="Exit-Code: 0 = Schema erkannt, 1 = kein eindeutiges Schema, 2 = Ordner nicht lesbar. "
               "Mit PROJECT_NUMBER_PATTERN=auto merkt sich Projektakte das beim ersten Lauf erkannte Schema und "
               "wechselt danach nie von selbst; meldet die Statusseite eine abweichende Erkennung, übernimmt "
               "--uebernehmen das jetzt erkannte Schema (ab dem nächsten Indexierungslauf).")
    ap.add_argument("pfad", nargs="?", metavar="PFAD", help="Projektordner (Standard: PROJECTS_DIR)")
    ap.add_argument("--tiefe", type=int, default=MAX_DEPTH, metavar="N",
                    help=f"höchstens N Ebenen unter PFAD durchsuchen (1–{MAX_DEPTH}, Standard {MAX_DEPTH})")
    ap.add_argument("--json", action="store_true", help="Ergebnis als JSON ausgeben")
    ap.add_argument("--data-dir", metavar="DIR",
                    help="Datenverzeichnis mit der Datenbank (Standard: DATA_DIR) – zeigt die Textmenge je Projekt")
    ap.add_argument("--ohne-dateien", action="store_true",
                    help="keine Dateistatistik (für sehr große Freigaben)")
    ap.add_argument("--max-dateien", type=int, default=MAX_FILES, metavar="N",
                    help=f"Dateistatistik nach N Dateien abbrechen (Standard {de_int(MAX_FILES)})")
    ap.add_argument("--max-ordner", type=int, default=MAX_FOLDERS, metavar="N",
                    help=f"höchstens N Ordner lesen (Standard {de_int(MAX_FOLDERS)})")
    ap.add_argument("--uebernehmen", action="store_true",
                    help="das jetzt erkannte Schema für PROJECT_NUMBER_PATTERN=auto speichern (ersetzt eine "
                         "frühere Erkennung)")
    args = ap.parse_args(argv)
    if not 1 <= args.tiefe <= MAX_DEPTH:
        ap.error(f"--tiefe muss zwischen 1 und {MAX_DEPTH} liegen")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if args.data_dir:
        settings = dataclasses.replace(settings, data_dir=Path(args.data_dir))
    root = Path(args.pfad) if args.pfad else settings.projects_dir
    if args.uebernehmen and root_key(root) != root_key(settings.projects_dir):
        ap.error(f"--uebernehmen gilt nur für PROJECTS_DIR ({settings.projects_dir}) – PFAD weglassen")
    progress = _progress_printer(True)
    analysis = analyse(root, max_depth=args.tiefe, max_folders=max(1, args.max_ordner), progress=progress)
    files = context = current = None
    if not analysis.problem:
        current = current_check(root, settings)
        if analysis.proposal and not args.ohne_dateien:
            files = file_statistics(analysis.projects, root, max_files=max(1, args.max_dateien), progress=progress)
        context = context_statistics(settings.db_path) or {"missing": str(settings.db_path)}
    _end_progress(True)
    accepted = None
    if args.uebernehmen and analysis.proposal is not None:
        db.init_db(settings.db_path)
        with db.session(settings.db_path) as conn:
            accepted = store_detection(conn, analysis.proposal, root)
    if args.json:
        data = {**analysis.to_dict(), "exit_code": analysis.exit_code, "current": current, "files": files,
                "context": context, "env": env_lines(analysis.proposal), "accepted": accepted}
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(format_report(analysis, current, files, context))
        if args.uebernehmen:
            print()
            if accepted:
                print(f"Übernommen: {accepted['pattern']}, Tiefe {accepted['depth']} – gilt mit "
                      "PROJECT_NUMBER_PATTERN=auto ab dem nächsten Indexierungslauf (Statusseite: „Jetzt neu "
                      "indexieren“). Projekte, die nicht zum neuen Schema passen, werden ausgeblendet und nach "
                      "der Karenzzeit aus dem Index entfernt.")
                if not settings.project_pattern_auto:
                    print(f"Hinweis: PROJECT_NUMBER_PATTERN ist fest eingestellt "
                          f"({settings.project_number_pattern}) – der gespeicherte Wert gilt erst mit "
                          "PROJECT_NUMBER_PATTERN=auto.")
            else:
                print("Nichts übernommen: kein eindeutiges Schema erkannt.")
    return analysis.exit_code


if __name__ == "__main__":
    sys.exit(main())
