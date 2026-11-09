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

Detection follows explicit rules (each one can be named in the report):

1. *Per level.* Only folders at the same depth below PROJECTS_DIR are compared with each other. The
   leading token of a name (``BS-2024-0137``, ``2024-017``, ``P12345``, ``2024_017``) is split into a
   letter prefix and digit groups; a candidate is (level, prefix, number of groups).
2. *Dates are no project numbers.* A bare year (1900–2099), a year range (2017-2018), year and month
   (2024-05, 05.2024), a full date (2024-05-21, 21.05.2024, 20240521) and short numbers without
   letters (00_Allgemein, 01 Schriftverkehr, 3 Pläne) are never candidates – they may be container
   names or exceptions.
3. *Internal numbering.* A folder whose number is its parent folder's number plus a sub-number of at
   most two digits (2024-017_01 below 2024-017 …, BS-2024-0137-01 below BS-2024-0137 …) is internal
   structure of that project, never a project. (A longer continuation, 1020 Stadt/1020-001 …, is a
   numbering of its own and decided by rule 4.)
4. *Containers.* A candidate whose folders consist mostly of folders of a deeper candidate (directly
   or through year folders) is a container level (customer numbers, …); the projects are the deepest
   such level whose folders are no containers. Folders with numbered internal subfolders are projects.
5. *Outliers do not widen a scheme.* A digit count or separator variant is accepted only when at
   least :data:`VARIANT_MIN_COUNT` folders and :data:`VARIANT_MIN_SHARE` of the candidate use it;
   others stay exceptions and are listed. Numbers without letters need at least 4 digits, or a
   running number of at least 3 digits after a first group (2024-017, 24-017). Numbers must be unique.
   A scheme of bare digits that could also match a year (or year range, or date) excludes those with
   a guard (``(?!(?:19|20)\\d\\d(?!\\d))\\d{4}``), so year folders never become projects.
6. *One scheme.* Different schemes are never joined automatically. Schemes below an archive folder
   (Archiv, Alt …) or an office folder (Angebote, Rechnungen, Verwaltung, Akquise, Vorlagen, Normen)
   yield to a scheme elsewhere; the report names them as a hint with the alternation to set by hand.
7. *Confidence.* A scheme is proposed (and stored in auto mode) only with at least
   :data:`MIN_PROJECTS` folders (below :data:`GOOD_PROJECTS` without any doubt), unique numbers (at
   most one or :data:`MAX_DUPLICATE_SHARE` duplicates; numbers that restart are no scheme at all), a
   real share of the folders next to them, and at least :data:`DOMINANCE` times as many folders as
   any competing scheme. Decisions use shares, not absolute sizes, so they do not change when the
   office grows; the inside of project folders is read from an evenly spread sample only.
8. *Links* are followed inside the share like the app's scan does, but every real folder is listed
   once (links to ancestors, loops and second paths to a folder are skipped).
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
MIN_PROJECTS = 3  # a scheme needs at least this many project folders …
GOOD_PROJECTS = 5  # … and below this many it must be beyond doubt (no outliers, duplicates, rivals)
MIN_SHARE = 0.3  # project folders: at least this share of the folders next to them …
MIN_SHARE_FEW = 0.5  # … and this share with fewer than GOOD_PROJECTS
MAX_DUPLICATE_SHARE = 0.1  # numbers must be unique: at most one duplicate or this share …
REPEAT_SHARE = 0.25  # … a candidate whose numbers repeat more often restarts its numbering (per year …)
VARIANT_MIN_COUNT = 3  # a digit count or separator variant needs this many folders …
VARIANT_MIN_SHARE = 0.1  # … and this share of its candidate, otherwise its folders stay exceptions
MAX_DIGIT_SPREAD = 2  # 3–5 digits at most in one digit group
DOMINANCE = 2.0  # the chosen scheme has at least this many times the folders of any rival scheme
YEAR_LOSS_SHARE = 0.05  # bare-digit numbers that look like years: tolerated as exceptions up to this share
SAMPLE_MEMBERS = 40  # folders of one candidate whose subfolders are listed (container or project?)
MAX_OPEN_PER_LEVEL = 500  # other folders opened per level (evenly spread sample beyond that)
MAX_GROUPS = 4  # digit groups of a leading token
DEPTH_MIN_SHARE = 0.1  # a level with single stray matches does not raise the proposed depth …
DEPTH_MIN_COUNT = 2  # … unless it holds at least this many projects
MAX_HINTS = 3  # further schemes named in the report
MAX_FILES = 200_000  # files counted by the file statistics (all projects together)
MAX_LISTED = 15  # rows of the text report per list
CHARS_PER_TOKEN = 3.5  # German text, typical tokenizer of current models
CONTEXT_WINDOWS = (("32k", 32_000), ("128k", 128_000), ("1M", 1_000_000))
CONTEXT_RESERVE = 4_000  # tokens kept free for instructions, the question and the answer
# a pattern that never matches: the shape of project numbers for search pages while none is known
NO_MATCH = r"(?!)"
# PROJECT_NUMBER_PATTERN=auto: a stored detection is checked again at most this often (and at every
# pass while a different detection is pending) – only to report a difference, never to switch; while
# nothing is stored, a detection that found no clear scheme is repeated at most this often as well
RECHECK_INTERVAL = timedelta(hours=1)

STORED_KEY = "project_layout_auto"  # meta: JSON of the accepted detection
DIFFERS_KEY = "project_layout_auto_differs"  # meta: JSON of a later detection that differs
CHECKED_KEY = "project_layout_auto_checked"  # meta: time of the last detection (UTC, ISO)
NONE_KEY = "project_layout_auto_none"  # meta: JSON of the last detection without a clear scheme

ACCEPT_HINT = "python -m projektakte.structure --uebernehmen"
EXIT_SCHEME, EXIT_NO_SCHEME, EXIT_UNREADABLE, EXIT_USAGE = 0, 1, 2, 3

try:
    from zoneinfo import ZoneInfo

    _LOCAL_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # noqa: BLE001 – no tz database: show UTC
    _LOCAL_TZ = timezone.utc


# ------------------------------------------------------------------------------------- formatting
def de_int(n: float | None) -> str:
    """1.234.567 (German thousands separator)."""
    return "–" if n is None else f"{round(n):,}".replace(",", ".")


def de_count(n: float, singular: str, plural: str) -> str:
    """'1 passender Ordner', '3 passende Ordner'."""
    return f"{de_int(n)} {singular if round(n) == 1 else plural}"


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


# ------------------------------------------------------------------------------------- leading numbers
_PREFIX = re.compile(r"([A-Za-z]{1,5})([-_ .]?)(?=[0-9])")
_DIGIT_RUN = re.compile(r"[0-9]+")
_SEPARATORS = "-_ ."
_SEP_ORDER = "-_. "
_SEP_ALONE = {"-": "-", "_": "_", ".": r"\.", " ": r"\s"}
_SEP_IN_CLASS = {"-": "-", "_": "_", ".": ".", " ": r"\s"}
_SEP_NAMES = {"-": "Bindestrich", "_": "Unterstrich", ".": "Punkt", " ": "Leerzeichen"}
# folders whose contents are no current projects: older numberings (rule 6) …
_ARCHIVE_NAME = re.compile(r"(?:archiv|alt(?:e|akten|projekte|bestand)?\b|old\b)", re.IGNORECASE)
# … and the office's own records with numberings of their own (offers, invoices, templates, norms)
_SIDE_NAME = re.compile(r"(?:angebot|rechnung|verwaltung|buchhaltung|akquise|vorlage|normen)", re.IGNORECASE)
_NAME_START = re.compile(r"^[\W\d_]+")
# German plural of the kinds of numbers that are no project numbers (rule 2)
_KIND_TEXT = {"Jahr": "Jahreszahlen", "Jahresbereich": "Jahresbereiche", "Monat": "Monatsangaben",
              "Datum": "Datumsangaben", "kurz": "kurze Gliederungsnummern"}
# guards against the date-like numbers a bare-digit scheme could match (rule 5)
_YEAR_GUARD = r"(?!(?:19|20)\d\d(?!\d))"
_DATE_GUARD = r"(?!(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d))"


@dataclass(frozen=True)
class _Token:
    """The leading number of a folder name: ``BS-2024-0137 Schule`` → prefix BS, groups 2024, 0137."""
    prefix: str  # letters before the digits, upper case ("" if none)
    psep: str  # separator between prefix and digits ("" if none)
    groups: tuple[str, ...]
    seps: tuple[str, ...]  # separators between the groups
    glued: bool  # a letter follows the last group directly (no number boundary there)

    def number(self, k: int) -> str:
        """The number made of the first ``k`` groups, as the scan reports it (upper case)."""
        out = self.prefix + self.psep + self.groups[0]
        for i in range(1, k):
            out += self.seps[i - 1] + self.groups[i]
        return out.upper()


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


def _year(g: str) -> bool:
    return len(g) == 4 and 1900 <= int(g) <= 2099


def _is_date(groups: tuple[str, ...]) -> bool:
    """YYYY-MM-DD, DD.MM.YYYY (also 1.5.2024), YY-MM-DD or DD.MM.YY (any separator)."""
    a, b, c = (int(g) for g in groups)
    la, lb, lc = (len(g) for g in groups)
    if (la, lb, lc) == (4, 2, 2):
        return 1900 <= a <= 2099 and 1 <= b <= 12 and 1 <= c <= 31
    if la <= 2 and lb <= 2 and lc == 4:
        return 1 <= a <= 31 and 1 <= b <= 12 and 1900 <= c <= 2099
    if (la, lb, lc) == (2, 2, 2):
        return 1 <= b <= 12 and (1 <= c <= 31 or 1 <= a <= 31)
    return False


def _number_kind(prefix: str, groups: tuple[str, ...]) -> str | None:
    """Why a leading number is no project number (rule 2) – 'Jahr', 'Jahresbereich', 'Monat',
    'Datum', 'kurz' – or None. A letter prefix makes any number a code (BS-2024, P12345)."""
    if prefix:
        return None
    n = len(groups)
    lens = [len(g) for g in groups]
    if n == 3 and _is_date(groups):
        return "Datum"
    if n == 2:
        a, b = groups
        if _year(a) and _year(b) and int(b) >= int(a):
            return "Jahresbereich"  # 2017-2018, 2005-2015
        if _year(a) and lens[1] == 2:
            return "Monat" if 1 <= int(b) <= 12 else "Jahresbereich" if int(b) == (int(a) + 1) % 100 else None
        if lens[0] <= 2 and 1 <= int(a) <= 12 and _year(b):
            return "Monat"  # 05.2024
    if n == 1:
        if _year(groups[0]):
            return "Jahr"
        g = groups[0]
        if len(g) == 8 and _is_date((g[:4], g[4:6], g[6:])):
            return "Datum"
    if max(lens) <= 2:
        return "kurz"  # 00_Allgemein, 01 Schriftverkehr, 3 Pläne, 1.2 Unterlagen
    return None


def _token_kind(tok: _Token | None) -> str | None:
    """The kind of a whole leading number that is no project number (for the explanation)."""
    if tok is None:
        return None
    return _number_kind(tok.prefix, tok.groups)


def _year_like(node: _Node) -> bool:
    """A year or year-range folder (2024, 2017-2018 …): a typical container, never a project."""
    tok = node.token
    return tok is not None and not tok.prefix and _number_kind("", tok.groups) in ("Jahr", "Jahresbereich")


def _sep_regex(chars: Iterable[str]) -> str:
    chars = set(chars)
    real = [c for c in _SEP_ORDER if c in chars]
    if not real:
        return ""
    body = _SEP_ALONE[real[0]] if len(real) == 1 else "[" + "".join(_SEP_IN_CLASS[c] for c in real) + "]"
    return body + ("?" if "" in chars else "")


def _sep_text(chars: Iterable[str]) -> str:
    chars = set(chars)
    real = [_SEP_NAMES[c] for c in _SEP_ORDER if c in chars]
    text = " oder ".join(real)
    return f"{text} (oder ohne)" if real and "" in chars else text


def _digits_regex(lo: int, hi: int) -> str:
    return rf"\d{{{lo}}}" if lo == hi else rf"\d{{{lo},{hi}}}"


def _digits_text(lo: int, hi: int) -> str:
    return "1 Ziffer" if lo == hi == 1 else f"{lo} Ziffern" if lo == hi else f"{lo}–{hi} Ziffern"


# ------------------------------------------------------------------------------------- folder tree
_UNSET = object()


@dataclass(eq=False)
class _Node:
    """A folder of the listed tree (identity semantics: usable in sets)."""
    name: str
    rel: str  # path below the root, "/"-separated
    depth: int  # 1 = directly below the root
    parent: _Node | None = None
    real: str = ""  # resolved path (links followed)
    via_link: bool = False  # reached through a link
    weight: float = 1.0  # folders it stands for: the inverse of the sampling on its path
    children: list[_Node] | None = None  # None: not listed
    unreadable: bool = False
    dropped: bool = False  # a second path to a folder listed already (link)
    _tok: object = field(default=_UNSET, repr=False)
    _branch: str | None = field(default=None, repr=False)

    @property
    def token(self) -> _Token | None:
        if self._tok is _UNSET:
            self._tok = _token(self.name) if self.depth else None
        return self._tok  # type: ignore[return-value]

    def ancestors(self) -> Iterable[_Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    @property
    def alive(self) -> bool:
        return not self.dropped and not any(a.dropped for a in self.ancestors())

    @property
    def branch(self) -> str:
        """'archive' or 'side' below an archive or office folder (the topmost one decides), else 'main'."""
        if self._branch is None:
            parent = self.parent
            if parent is None or parent.depth == 0:
                self._branch = "main"
            elif parent.branch != "main":
                self._branch = parent.branch
            else:
                name = _NAME_START.sub("", parent.name)
                self._branch = ("archive" if _ARCHIVE_NAME.match(name) else "side" if _SIDE_NAME.match(name)
                                else "main")
        return self._branch


@dataclass
class _Tree:
    root: _Node
    by_level: dict[int, list[_Node]] = field(default_factory=dict)  # listed folders per depth
    by_real: dict[str, _Node] = field(default_factory=dict)
    cands: dict[int, tuple[list, list]] = field(default_factory=dict)  # level → (valid, invalid)
    listed: int = 0
    listings: int = 0
    truncated: bool = False
    any_dropped: bool = False
    unreadable: list[str] = field(default_factory=list)

    def level(self, depth: int) -> list[_Node]:
        nodes = self.by_level.get(depth, [])
        return [n for n in nodes if n.alive] if self.any_dropped else list(nodes)

    @property
    def nodes(self) -> list[_Node]:
        return [n for d in sorted(self.by_level) for n in self.level(d)]


def _drop(tree: _Tree, node: _Node) -> None:
    node.dropped = True
    tree.any_dropped = True
    if node.parent is not None and node.parent.children is not None:
        node.parent.children = [c for c in node.parent.children if c is not node]


def _list(tree: _Tree, node: _Node, root_path: Path, boundary: Path, max_folders: int, factor: float) -> None:
    """List the subfolders ``scan_projects`` considers – hidden ones and links whose target is
    missing, a loop or outside ``boundary`` are skipped – sorted by name. Every real folder is listed
    once: a link to an ancestor or to a folder listed already is skipped, and a folder reached
    through a link gives way when it is found on its real path (rule 8). Raises OSError for the root."""
    path = root_path / node.rel if node.rel else root_path
    entries: list[tuple[str, bool, str]] = []
    cut = False
    tree.listings += 1
    try:
        with os.scandir(path) as it:
            for n, entry in enumerate(it):
                if n >= max_folders:
                    cut = True
                    break
                if entry.name.startswith("."):
                    continue
                try:
                    link = entry.is_symlink()
                    if link and link_problem(Path(entry.path), boundary):
                        continue
                    if entry.is_dir():
                        entries.append((entry.name, link, entry.path))
                except OSError:
                    continue
    except OSError:
        if node is tree.root:
            raise
        node.unreadable, node.children = True, []
        tree.unreadable.append(node.rel)
        return
    entries.sort()
    room = max(0, max_folders - tree.listed)
    if len(entries) > room:
        entries, cut = entries[:room], True
    tree.truncated |= cut
    children: list[_Node] = []
    for name, link, entry_path in entries:
        if link:
            real = os.path.realpath(entry_path)
            if real == node.real or node.real.startswith(real.rstrip(os.sep) + os.sep):
                continue  # a link to this folder or one of its ancestors: a loop
        else:
            real = node.real.rstrip(os.sep) + os.sep + name
        via = node.via_link or link
        seen = tree.by_real.get(real)
        if seen is not None and seen.alive:
            if seen.via_link and not via:
                _drop(tree, seen)  # the folder on its real path wins over a link to it
            else:
                continue  # the same folder again, through a link
        child = _Node(name, f"{node.rel}/{name}" if node.rel else name, node.depth + 1, node, real=real,
                      via_link=via, weight=node.weight * factor)
        tree.by_real[real] = child
        children.append(child)
    node.children = children
    tree.by_level.setdefault(node.depth + 1, []).extend(children)
    tree.listed += len(children)


def _sample(items: list, n: int) -> list:
    """At most ``n`` of ``items``, evenly spread (deterministic)."""
    if len(items) <= n:
        return list(items)
    return [items[(i * len(items)) // n] for i in range(n)]


def _member_ancestor(node: _Node, owner: dict) -> _Node | None:
    for a in node.ancestors():
        if a in owner:
            return a
    return None


def _explore(root_path: Path, max_depth: int, max_folders: int,
             progress: Callable[[str], None] | None) -> _Tree:
    """List the folder tree level by level – folder names only. Per level the candidate schemes are
    determined (rule 1); of the folders of a candidate only an evenly spread sample is opened (are
    they projects or containers of projects?), and inside them only year folders (customer/year/
    project); everything else is opened up to :data:`MAX_OPEN_PER_LEVEL` per level. So the reading
    stays about as cheap as the app's own scan, and the shares the decisions rest on do not depend
    on the size of the share. Raises OSError if the root is unreadable."""
    boundary = root_path.resolve()
    tree = _Tree(_Node("", "", 0, real=str(boundary)))
    _list(tree, tree.root, root_path, boundary, max_folders, 1.0)
    owner: dict[_Node, _Cand] = {}
    last_report = time.monotonic()
    for level in range(1, max_depth + 1):
        nodes = tree.level(level)
        valid, invalid = _level_candidates(nodes)
        tree.cands[level] = (valid, invalid)
        for cand in valid:
            owner.update((m, cand) for m in cand.members)
        if level == max_depth or not nodes or tree.truncated:
            continue
        todo: list[tuple[_Node, float]] = []
        for cand in valid:
            sample = _sample(cand.members, SAMPLE_MEMBERS)
            todo += [(m, len(cand.members) / len(sample)) for m in sample]
        free: list[_Node] = []
        for node in nodes:
            if node in owner:
                continue
            anc = _member_ancestor(node, owner)
            if anc is None:
                free.append(node)
            elif node.parent is anc and _year_like(node):
                todo.append((node, 1.0))  # customer 1020/2024/2024-001 …: look one level further
        sample = _sample(free, MAX_OPEN_PER_LEVEL)
        todo += [(n, len(free) / len(sample)) for n in sample]
        todo.sort(key=lambda t: t[0].rel)
        for node, factor in todo:
            if tree.listed >= max_folders:
                tree.truncated = True
                break
            _list(tree, node, root_path, boundary, max_folders, factor)
            if progress and time.monotonic() - last_report >= 1.0:
                last_report = time.monotonic()
                progress(f"{de_int(tree.listed)} Ordner gelesen (Ebene {level + 1})")
    if tree.any_dropped:  # a link listed before its target: decide on the folders that remain
        tree.cands = {level: _level_candidates(tree.level(level)) for level in tree.cands}
    return tree


# ------------------------------------------------------------------------------------- candidates per level
@dataclass(eq=False)
class _Cand:
    """Folders of one level whose leading numbers have the same prefix and number of groups."""
    level: int
    prefix: str
    k: int
    items: list[_Node]  # all folders of the group (members and outliers)
    members: list[_Node]
    numbers: list[str]  # of the members, as the scan reports them
    outliers: list[_Node]  # a digit count or separator variant without real backing (rule 5)
    pseps: frozenset[str]
    seps: tuple[frozenset[str], ...]
    lengths: tuple[tuple[int, int], ...]
    problem: str | None = None
    outer: _Cand | None = None  # the candidate whose folders most of these lie in
    contains: list[_Cand] = field(default_factory=list)  # deeper candidates it is a container of

    @property
    def est(self) -> float:
        """Estimated number of folders (sampling on the way down taken into account)."""
        return sum(m.weight for m in self.members)

    def guards(self) -> list[tuple[str, str]]:
        """(regex, German note) for the date-like numbers this scheme could match (rule 5)."""
        if self.prefix:
            return []
        out = []
        if self.k == 1:
            lo, hi = self.lengths[0]
            if lo <= 4 <= hi:
                out.append((_YEAR_GUARD, "keine Jahreszahl 1900–2099"))
            if lo <= 8 <= hi:
                out.append((_DATE_GUARD, "kein Datum"))
        elif self.k == 2:
            (lo0, hi0), (lo1, hi1) = self.lengths
            if lo0 <= 4 <= hi0 and lo1 <= 4 <= hi1:
                out.append((rf"(?!(?:19|20)\d\d{_sep_regex(self.seps[0])}(?:19|20)\d\d(?!\d))",
                            "kein Jahresbereich"))
        return out

    @property
    def pattern(self) -> str:
        out = "".join(g for g, _ in self.guards())
        out += self.prefix + (_sep_regex(self.pseps) if self.prefix else "")
        for i, (lo, hi) in enumerate(self.lengths):
            out += (_sep_regex(self.seps[i - 1]) if i else "") + _digits_regex(lo, hi)
        return out

    @property
    def text(self) -> str:
        parts = []
        if self.prefix:
            parts.append(f"„{self.prefix}“")
            if sep := _sep_text(self.pseps):
                parts.append(sep)
        for i, (lo, hi) in enumerate(self.lengths):
            if i:
                parts.append(_sep_text(self.seps[i - 1]))
            parts.append(_digits_text(lo, hi))
        notes = [note for _, note in self.guards()]
        return ", ".join(parts) + (f" ({', '.join(notes)})" if notes else "")

    @property
    def specificity(self) -> int:
        return (10 if self.prefix else 0) + self.k


def _internal(node: _Node) -> bool:
    """Rule 3: the folder's number is its parent folder's number plus sub-numbers of at most two
    digits (2024-017_01 below 2024-017 …, or the same number again) – internal structure."""
    parent = node.parent
    if parent is None or parent.depth == 0:
        return False
    pt, t = parent.token, node.token
    if pt is None or t is None or pt.prefix != t.prefix:
        return False
    for j in range(min(len(pt.groups), len(t.groups)), 0, -1):
        if t.groups[:j] != pt.groups[:j] or _number_kind(pt.prefix, pt.groups[:j]):
            continue
        if all(len(g) <= 2 for g in t.groups[j:]):
            return True
    return False


def _accepted(values: list, need: int) -> set:
    """The values with real backing (rule 5); the most frequent one always."""
    counts = Counter(values)
    main = counts.most_common(1)[0][0]
    return {v for v, c in counts.items() if c >= need} | {main}


def _digits_problem(prefix: str, k: int, lengths: tuple[tuple[int, int], ...]) -> str | None:
    lows = [lo for lo, _ in lengths]
    if any(hi - lo > MAX_DIGIT_SPREAD for lo, hi in lengths):
        return "Ziffernzahl zu uneinheitlich"
    if prefix:
        if sum(lows) < (3 if len(prefix) <= 3 else 4):  # a long prefix is rather a word ("Haus 12")
            return "zu wenige Ziffern nach den Buchstaben"
        return None
    if k == 1:
        return "zu kurz (eine Zahl ohne Buchstaben braucht mindestens 4 Ziffern)" if lows[0] < 4 else None
    if lows[-1] < 3 or sum(lows) < 5:
        return "zu kurz (laufende Nummer mit weniger als 3 Ziffern)"
    if k == 3:
        (lo0, hi0), (lo1, hi1), (lo2, hi2) = lengths
        if lo0 <= 2 <= hi0 and lo1 <= 2 <= hi1 and lo2 <= 4 <= hi2:
            return "hat die Form eines Datums (TT.MM.JJJJ)"
    return None


def _build(level: int, prefix: str, k: int, items: list[tuple[_Node, _Token]]) -> _Cand:
    need = max(VARIANT_MIN_COUNT, math.ceil(VARIANT_MIN_SHARE * len(items)))
    pseps = _accepted([t.psep for _, t in items], need) if prefix else {""}
    seps = [_accepted([t.seps[i] for _, t in items], need) for i in range(k - 1)]
    lengths = []
    for i in range(k):
        ok = _accepted([len(t.groups[i]) for _, t in items], need)
        lengths.append((min(ok), max(ok)))
    members, numbers, outliers = [], [], []
    for node, t in items:
        fits = (t.psep in pseps and all(t.seps[i] in seps[i] for i in range(k - 1))
                and all(lo <= len(t.groups[i]) <= hi for i, (lo, hi) in enumerate(lengths)))
        if fits:
            members.append(node)
            numbers.append(t.number(k))
        else:
            outliers.append(node)
    cand = _Cand(level, prefix, k, [n for n, _ in items], members, numbers, outliers, frozenset(pseps),
                 tuple(frozenset(s) for s in seps), tuple(lengths))
    dups = len(numbers) - len(set(numbers))
    if problem := _digits_problem(prefix, k, cand.lengths):
        cand.problem = problem
    elif prefix and k == 1 and all(_year(node.token.groups[0]) for node in members):
        cand.problem = "ein Wort mit Jahreszahl, keine Projektnummer"
    elif len(members) < MIN_PROJECTS:
        cand.problem = f"nur {de_count(len(members), 'passender Ordner', 'passende Ordner')}"
    elif dups > max(1, REPEAT_SHARE * len(members)):
        cand.problem = f"Nummern wiederholen sich ({dups} von {len(members)} doppelt)"
    return cand


def _level_candidates(nodes: list[_Node]) -> tuple[list[_Cand], list[_Cand]]:
    """Valid and invalid candidate schemes among folders of one level (rules 1–3, 5). Folders below
    an archive or office folder form candidates of their own (an older numbering is no variant of
    the current one, rule 6). Of several readings of the same folders (BS-2024 or BS-2024-0137) the
    valid one with most folders wins, with equal numbers the shorter one."""
    groups: dict[tuple[str, int, str], list[tuple[_Node, _Token]]] = {}
    for node in nodes:
        tok = node.token
        if tok is None or _internal(node):
            continue
        for k in range(1, len(tok.groups) + 1):
            if k == len(tok.groups) and tok.glued:
                break  # "2024-017a": no number boundary after the last group
            if _number_kind(tok.prefix, tok.groups[:k]):
                continue
            groups.setdefault((tok.prefix, k, node.branch), []).append((node, tok))
    level = nodes[0].depth if nodes else 0
    built = sorted((_build(level, prefix, k, items) for (prefix, k, _), items in groups.items()),
                   key=lambda c: (c.problem is not None, -len(c.members), c.k, c.prefix))
    valid: list[_Cand] = []
    invalid: list[_Cand] = []
    for c in built:
        mine = set(c.items if c.problem else c.members)
        if any(c.prefix == v.prefix and len(mine & set(v.members)) * 2 >= len(mine) for v in valid):
            continue  # the same folders read with more or fewer digit groups
        (invalid if c.problem else valid).append(c)
    return valid, invalid


# ------------------------------------------------------------------------------------- choosing the scheme
def _container_of(outer: _Cand, inner: _Cand) -> bool:
    """Rule 4: are the folders of ``outer`` containers of ``inner``'s folders? Yes if most of them
    consist mostly of such folders (directly or in year folders). Folders with numbered internal
    subfolders are projects."""
    inside = set(inner.members)
    opened = [m for m in outer.members if m.children is not None and not m.unreadable]
    if not opened:
        return False
    if 2 * sum(1 for m in opened if any(_internal(c) for c in m.children or ())) >= len(opened):
        return False
    shares = []
    for m in opened:
        kids = m.children or []
        bearing = sum(1 for c in kids if c in inside or (c.children and any(g in inside for g in c.children)))
        shares.append(bearing / len(kids) if kids else 0.0)
    return statistics.median(shares) >= 0.5


def _branch(nodes: list[_Node]) -> str:
    """'archive' or 'side' if most folders lie below an archive or office folder, else 'main'."""
    kind, n = Counter(node.branch for node in nodes).most_common(1)[0] if nodes else ("main", 0)
    return kind if 2 * n > len(nodes) else "main"


def _where(nodes: list[_Node]) -> str:
    """The common parent folder of ``nodes`` ('Archiv/2005-2015') – '' at the top or if spread."""
    paths = [n.parent.rel.split("/") if n.parent is not None and n.parent.rel else [] for n in nodes]
    if not paths:
        return ""
    common = paths[0]
    for p in paths[1:]:
        i = 0
        while i < min(len(common), len(p)) and common[i] == p[i]:
            i += 1
        common = common[:i]
    return "/".join(common)


@dataclass(eq=False)
class _Scheme:
    """Project-level candidates with the same expression (one scheme, possibly at several levels)."""
    pattern: str
    cands: list[_Cand]
    branch: str = "main"

    @property
    def members(self) -> list[_Node]:
        return [m for c in self.cands for m in c.members]

    @property
    def est(self) -> float:
        return sum(c.est for c in self.cands)

    @property
    def text(self) -> str:
        return self.cands[0].text

    @property
    def outliers(self) -> list[_Node]:
        return [o for c in self.cands for o in c.outliers]

    @property
    def guarded_years(self) -> bool:
        return any(g == _YEAR_GUARD for c in self.cands for g, _ in c.guards())

    @property
    def specificity(self) -> int:
        return self.cands[0].specificity


@dataclass
class _Choice:
    best: _Scheme | None
    reason: str | None
    schemes: list[_Scheme]  # project-level schemes, best first
    candidates: list[dict]  # all candidates with their verdict (report)
    others: list[_Scheme] = field(default_factory=list)  # further schemes for the hint


def _cand_info(c: _Cand, verdict: str, reason: str | None = None) -> dict:
    where = _where(c.members or c.items)
    return {"pattern": c.pattern, "description": c.text, "level": c.level, "folders": len(c.members),
            "estimated": round(c.est), "where": where,
            "examples": [n.name for n in (c.members or c.items)[:3]], "verdict": verdict, "reason": reason}


def _choose(tree: _Tree) -> _Choice:
    valid = [c for level in sorted(tree.cands) for c in tree.cands[level][0]]
    invalid = [c for level in sorted(tree.cands) for c in tree.cands[level][1]]
    owner = {m: c for c in valid for m in c.members}
    for c in valid:  # the nearest candidate around most of its folders
        votes = Counter(owner[a] for m in c.members if (a := _member_ancestor(m, owner)) is not None)
        c.outer = None
        if votes:
            outer, n = votes.most_common(1)[0]
            if 2 * n >= len(c.members):
                c.outer = outer
    for c in valid:
        c.contains = [x for x in valid if x.outer is c and _container_of(c, x)]
    infos: list[dict] = []
    level_ok: list[_Cand] = []
    for c in valid:
        if c.contains:
            inner = max(c.contains, key=lambda x: len(x.members))
            infos.append(_cand_info(c, "Sammelordner", f"enthalten die Projektordner ({inner.pattern}, Ebene "
                                                       f"{inner.level})"))
            continue
        x, outer = c, c.outer
        while outer is not None and x in outer.contains:
            x, outer = outer, outer.outer
        if outer is not None:
            infos.append(_cand_info(c, "in Projektordnern", f"liegen in den Ordnern von {outer.pattern} "
                                                            f"(Ebene {outer.level}) und sind keine Projekte"))
            continue
        level_ok.append(c)
    by_pattern: dict[str, _Scheme] = {}
    for c in level_ok:
        by_pattern.setdefault(c.pattern, _Scheme(c.pattern, [])).cands.append(c)
    schemes = list(by_pattern.values())
    for s in schemes:
        s.branch = _branch(s.members)
    schemes.sort(key=lambda s: (-s.est, -s.specificity, s.pattern))
    # numbered folders without a valid scheme (numbers that restart, too few digits …) outside project
    # folders: they make a share unclear just as a second scheme does
    unclear = [c for c in invalid if len(c.items) >= MIN_PROJECTS and not c.problem.startswith("nur ")
               and 2 * sum(1 for n in c.items if _member_ancestor(n, owner) is None) > len(c.items)]
    unclear_branch = {id(c): _branch(c.items) for c in unclear}
    branch = "main" if any(s.branch == "main" for s in schemes) else "archive"
    pool = [s for s in schemes if s.branch == branch]
    blocking = [c for c in unclear if unclear_branch[id(c)] in ("main", branch)]
    best, reason = (pool[0] if pool else None), None
    if best is not None and branch == "archive" and any(unclear_branch[id(c)] == "main" for c in blocking):
        c = max((c for c in blocking if unclear_branch[id(c)] == "main"), key=lambda c: len(c.items))
        reason = (f"Kein Nummernschema ist eindeutig genug – {c.pattern} (Ebene {c.level}, "
                  f"{de_count(len(c.items), 'Ordner', 'Ordner')}{_where_text(c.items)}): {c.problem}. Das Schema "
                  f"{best.pattern}{_scheme_where(best)} gilt als ältere Nummerierung und wird nicht allein "
                  "vorgeschlagen.")
        best = None
    elif best is not None:
        rivals = [(s.est, s.pattern, s.members, None) for s in pool[1:]]
        rivals += [(sum(n.weight for n in c.items), c.pattern, c.items, c.problem) for c in blocking]
        est, pattern, nodes, problem = max(rivals, key=lambda r: r[0], default=(0, "", [], None))
        if est * DOMINANCE > best.est:
            reason = (f"Zwei Nummernschemata kommen etwa gleich oft vor: {best.pattern} "
                      f"({_scheme_count(best)}{_scheme_where(best)}, z. B. {best.members[0].name}) und "
                      f"{pattern} ({_count_text(est, len(nodes))}{_where_text(nodes)}, z. B. {nodes[0].name}"
                      + (f"; {problem}" if problem else "") + ") – welches die Projekte sind, lässt sich nicht sicher "
                      "sagen.")
            best = None
    elif schemes:
        side = schemes[0]
        reason = (f"Nummerierte Ordner gibt es nur in Nebenablagen ({side.pattern}{_scheme_where(side)}) – "
                  "Angebote, Rechnungen und Vorlagen sind keine Projekte.")
    others = [s for s in schemes if s is not best]
    for s in schemes:
        verdict = "gewählt" if s is best else "Hinweis" if best is not None else "Kandidat"
        why = None
        if s is not best and best is not None:
            why = {"archive": "unter einem Archivordner – ältere Nummerierung?",
                   "side": "in einer Nebenablage (Angebote, Rechnungen …)"}.get(
                s.branch, f"seltener als {best.pattern}")
        infos += [_cand_info(c, verdict, why) for c in s.cands]
    infos += [_cand_info(c, "abgelehnt", c.problem) for c in invalid if len(c.items) > 1]
    order = {"gewählt": 0, "Kandidat": 1, "Hinweis": 2, "Sammelordner": 3, "in Projektordnern": 4, "abgelehnt": 5}
    infos.sort(key=lambda i: (order[i["verdict"]], -i["estimated"], i["level"]))
    return _Choice(best, reason, schemes, infos, others)


def _count_text(est: float, listed: int) -> str:
    return de_count(est, "Ordner", "Ordner") if round(est) == listed else f"etwa {de_int(est)} Ordner"


def _where_text(nodes: list[_Node]) -> str:
    where = _where(nodes)
    return f" in {where}" if where else ""


def _scheme_count(s: _Scheme) -> str:
    return _count_text(s.est, len(s.members))


def _scheme_where(s: _Scheme) -> str:
    return _where_text(s.members)


@dataclass
class _Scan:
    """What ``scan_projects`` would find in the listed tree."""
    matches: list[tuple[_Node, str]]
    duplicates: list[tuple[str, _Node, _Node]]
    exceptions: list[_Node]  # folders without the number next to project folders (Archiv, _Vorlagen …)


def _simulate(root: _Node, head: re.Pattern, max_depth: int) -> _Scan:
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
    return _Scan(matches, duplicates, exceptions)


def _proposed_depth(depths: Counter) -> int:
    """The deepest level that holds a real share of the projects – a single stray match far below
    (a template folder named like a project) does not make every scan look that deep. (The caller
    raises 1 to the default 2 when that finds the same folders.)"""
    total = sum(depths.values())
    levels = [d for d, n in depths.items() if n >= DEPTH_MIN_COUNT or n >= DEPTH_MIN_SHARE * total]
    return max(levels or depths)


def _depth_for(root: _Node, head: re.Pattern, max_depth: int) -> tuple[int, _Scan]:
    everything = _simulate(root, head, max_depth)
    if not everything.matches:
        return min(max_depth, DEFAULT_PROJECT_FOLDER_DEPTH), everything
    depth = _proposed_depth(Counter(n.depth for n, _ in everything.matches))
    if depth < DEFAULT_PROJECT_FOLDER_DEPTH <= max_depth:
        # the default finds the very same folders today and also projects put into a year folder later
        at_default = _simulate(root, head, DEFAULT_PROJECT_FOLDER_DEPTH)
        if {n for _, n in at_default.matches} == {n for _, n in _simulate(root, head, depth).matches}:
            depth = DEFAULT_PROJECT_FOLDER_DEPTH
    return depth, everything


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
    second_scheme: dict | None  # the first of ``hints``
    unreadable: list[str]
    outliers: list[str] = field(default_factory=list)  # same kind of number, variant without backing
    hints: list[dict] = field(default_factory=list)  # further schemes (archive, offers …) with alternation


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
    candidates: list[dict] = field(default_factory=list)  # every scheme considered, with its verdict
    rejected: list[dict] = field(default_factory=list)  # pattern, reason
    alternatives: list[dict] = field(default_factory=list)  # further schemes (same as proposal.hints)
    seconds: float = 0.0
    listings: int = 0  # folders opened
    projects: list[ProjectFolder] = field(default_factory=list, repr=False)

    @property
    def exit_code(self) -> int:
        return EXIT_UNREADABLE if self.problem else EXIT_SCHEME if self.proposal else EXIT_NO_SCHEME

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
    for depth in sorted(tree.by_level):
        nodes = tree.level(depth)
        if nodes:
            names = list(dict.fromkeys(n.name for n in nodes))
            numbered = [name for name in names if _token(name)]
            out.append({"level": depth, "folders": len(nodes), "with_number": sum(1 for n in nodes if n.token),
                        "examples": (numbered[:3] + [name for name in names if name not in numbered[:3]])[:6]})
    return out


def _no_scheme_reason(tree: _Tree, choice: _Choice) -> str:
    nodes = tree.nodes
    if not nodes:
        return "Der Ordner enthält keine Unterordner – ist die Freigabe eingebunden?"
    numbered = [n for n in nodes if n.token]
    if not numbered:
        return "Kein Ordnername beginnt mit einer Nummer."
    if choice.reason:
        return choice.reason
    invalid = [c for level in sorted(tree.cands) for c in tree.cands[level][1]]
    if not invalid and not choice.candidates:
        kinds: dict[str, list[str]] = {}
        for n in numbered:
            if kind := _token_kind(n.token):
                kinds.setdefault(kind, []).append(n.name)
        if kinds:
            parts = [f"{_KIND_TEXT[k]} ({', '.join(v[:2])}{' …' if len(v) > 2 else ''})"
                     for k, v in sorted(kinds.items(), key=lambda kv: -len(kv[1]))]
            return ("Die Nummern am Anfang der Ordnernamen sind keine Projektnummern: " + "; ".join(parts) + ".")
    serious = [c for c in invalid if len(c.items) >= MIN_PROJECTS and not c.problem.startswith("nur ")]
    if serious:
        reasons = "; ".join(f"{c.pattern} (Ebene {c.level}): {c.problem}" for c in serious[:3])
        return f"Kein Nummernschema ist eindeutig genug – {reasons}."
    return (f"Zu wenige Ordner mit gleich aufgebauter Nummer am Anfang (mindestens {MIN_PROJECTS} auf einer "
            "Ebene nötig, z. B. 2024-017 Kita, 2024-018 Schule …).")


def _hint(tree: _Tree, best: _Scheme, other: _Scheme, max_depth: int) -> dict:
    combined = f"(?:{best.pattern}|{other.pattern})"
    depth, _ = _depth_for(tree.root, project_head(combined), max_depth)
    return {"pattern": other.pattern, "description": other.text, "folders": round(other.est),
            "estimated": round(other.est) != len(other.members), "where": _where(other.members),
            "branch": other.branch, "examples": [n.name for n in other.members[:3]],
            "combined": combined, "depth": depth}


def _reject(result: Analysis, reason: str, started: float) -> Analysis:
    result.reason = reason
    for info in result.candidates:
        if info["verdict"] == "gewählt":
            info["verdict"], info["reason"] = "Kandidat", "zu unsicher (siehe oben)"
    result.seconds = round(time.monotonic() - started, 2)
    return result


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
    result.folders, result.truncated, result.listings = tree.listed, tree.truncated, tree.listings
    result.unreadable = tree.unreadable[:MAX_LISTED * 4]
    result.levels = _level_summary(tree)
    choice = _choose(tree)
    result.candidates = choice.candidates[:12]
    result.rejected = [{"pattern": i["pattern"], "reason": i["reason"]} for i in choice.candidates
                       if i["verdict"] in ("abgelehnt", "Sammelordner", "in Projektordnern")][:8]
    best = choice.best
    if best is None:
        return _reject(result, _no_scheme_reason(tree, choice), started)
    head = project_head(best.pattern)
    depth, everything = _depth_for(tree.root, head, max_depth)
    members = set(best.members)
    holding = {a for m in members for a in m.ancestors()}
    if clash := [n for n, _ in everything.matches if n in holding]:
        return _reject(result, f"Das Schema {best.pattern} passt auch auf die Ordner, in denen die Projektordner "
                               f"liegen ({', '.join(n.rel for n in clash[:3])}) – diese würden selbst als Projekte "
                               "gelesen.", started)
    final = _simulate(tree.root, head, depth)
    if best.guarded_years:
        # folders at the projects' level whose number is a year (1999 Projekt): next to projects or in
        # a year folder of their own (2024/1901 …, 2024/1902 …) – the guard keeps them out
        levels = {c.level for c in best.cands}
        lost = [n for level in sorted(levels) for n in tree.level(level)
                if _token_kind(n.token) == "Jahr" and n not in holding and n.parent is not None
                and (n.parent in holding or _year_like(n.parent))]
        if len(lost) > max(2, YEAR_LOSS_SHARE * len(best.members)):
            return _reject(result, f"Projektnummern und Jahreszahlen sind nicht zu unterscheiden: "
                                   f"{de_count(len(lost), 'Ordner beginnt', 'Ordner beginnen')} mit einer Zahl "
                                   f"zwischen 1900 und 2099 (z. B. {', '.join(n.rel for n in lost[:3])}), die nach "
                                   f"dem Schema {best.pattern} als Jahreszahl gilt und nicht gelesen würde.", started)
    n, ex = len(final.matches), len(final.exceptions)
    need = MIN_SHARE_FEW if n < GOOD_PROJECTS else MIN_SHARE
    if n + ex and n < need * (n + ex):
        return _reject(result, f"Nur {round(100 * n / (n + ex))} % der Ordner neben den Projektordnern passen zum "
                               f"Schema {best.pattern} ({de_count(n, 'passender', 'passende')}, "
                               f"{de_count(ex, 'anderer', 'andere')}) – zu wenig für ein sicheres Schema.", started)
    unreadable: list[Path] = []
    duplicates: list[tuple[str, Path, Path]] = []
    found = scan_projects(root, best.pattern, depth, unreadable=unreadable, duplicates=duplicates)
    # the same folder reached through a link is no second project (the scan skips it as well)
    duplicates = [d for d in duplicates if os.path.realpath(d[1]) != os.path.realpath(d[2])]
    if len(found) < MIN_PROJECTS:  # the share changed while it was read
        return _reject(result, f"Das Schema {best.pattern} fand beim Gegenprüfen nur "
                               f"{de_count(len(found), 'Projektordner', 'Projektordner')} – Freigabe vollständig "
                               "eingebunden?", started)
    if len(duplicates) > max(1, MAX_DUPLICATE_SHARE * len(found)):
        examples = "; ".join(f"{_rel(a, root)} und {_rel(b, root)}" for _, a, b in duplicates[:2])
        return _reject(result, f"Viele doppelte Nummern mit dem Schema {best.pattern} ({len(duplicates)} bei "
                               f"{de_count(len(found), 'Projektordner', 'Projektordnern')}, z. B. {examples}) – "
                               "gelesen würde jeweils nur der erste Ordner. Nummern eindeutig machen oder "
                               "PROJECT_NUMBER_PATTERN von Hand einstellen.", started)
    doubts = [text for text, present in (("doppelte Nummern", duplicates), ("abweichende Nummern", best.outliers),
                                         ("ein weiteres Nummernschema", choice.others)) if present]
    if len(found) < GOOD_PROJECTS and doubts:
        return _reject(result, f"Nur {de_count(len(found), 'Projektordner', 'Projektordner')} mit dem Schema "
                               f"{best.pattern}, und daneben gibt es {' und '.join(doubts)} – zu wenig für ein "
                               "sicheres Schema.", started)
    parents = Counter(_rel(p.path.parent, root) for p in found if p.path.parent != root)
    hints = [_hint(tree, best, other, max_depth) for other in choice.others[:MAX_HINTS]]
    result.projects = found
    result.alternatives = hints
    result.proposal = Proposal(
        pattern=best.pattern, depth=depth, description=best.text, projects=len(found),
        examples=[{"number": p.number, "name": p.name, "path": _rel(p.path, root)} for p in found[:MAX_LISTED]],
        levels=dict(sorted(Counter(len(_rel(p.path, root).split("/")) for p in found).items())),
        parents=[[k, v] for k, v in parents.most_common(8)],
        exceptions=[n.rel for n in final.exceptions[:MAX_LISTED * 2]], exceptions_total=len(final.exceptions),
        duplicates=[{"number": num, "path": _rel(a, root), "duplicate": _rel(b, root)}
                    for num, a, b in duplicates[:MAX_LISTED]],
        deeper=[node.rel for node, _ in everything.matches if node.depth > depth][:MAX_LISTED],
        second_scheme=hints[0] if hints else None,
        unreadable=[_rel(p, root) for p in unreadable[:MAX_LISTED]],
        outliers=[o.rel for o in best.outliers[:MAX_LISTED]], hints=hints)
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
    next_check: str | None = None  # auto mode without a clear scheme: the next detection (UTC, ISO)

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
            if not self.auto:
                return "unbrauchbar – Projekte werden nicht abgeglichen"
            when = (f"nächste Prüfung frühestens {local_time_text(self.next_check)}" if self.next_check
                    else "beim nächsten Indexierungslauf")
            return f"automatisch – noch nicht erkannt ({when})"
        depth = f"Tiefe {self.depth}" + (" (automatisch)" if self.auto and self.depth_auto else "")
        return f"{self.pattern} ({self.source}), {depth}"

    def to_dict(self) -> dict:
        return {"pattern": self.pattern, "depth": self.depth, "auto": self.auto, "depth_auto": self.depth_auto,
                "source": self.source, "depth_text": depth_text(self.depth), "detected_at": self.detected_at,
                "examples": list(self.examples), "projects_at_detection": self.projects,
                "problem": self.problem, "warning": self.warning, "next_check": self.next_check,
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


def _failed_detection(conn: sqlite3.Connection | None, projects_dir: Path | str) -> dict | None:
    """The last detection without a clear scheme for this PROJECTS_DIR (reason, time) – or None."""
    rec = _load(conn, NONE_KEY)
    if rec is None or rec.get("root") != root_key(projects_dir) or not isinstance(rec.get("reason"), str):
        return None
    return rec


def _next_check(rec: dict) -> str | None:
    when = _parse_time(rec.get("checked_at"))
    return _iso(when + RECHECK_INTERVAL) if when else None


def _failed_problem(rec: dict) -> str:
    return (f"Kein eindeutiges Projektnummern-Schema in {rec.get('root')} erkannt (PROJECT_NUMBER_PATTERN=auto, "
            f"Prüfung {local_time_text(rec.get('checked_at'))}, das Schema ist also noch nicht erkannt): "
            f"{rec['reason']} Nächste Prüfung frühestens {local_time_text(_next_check(rec))}. Bitte "
            "PROJECT_NUMBER_PATTERN einstellen (Vorschlag und Ordnerübersicht: python -m projektakte.structure) "
            f"oder, sobald die Ordner stimmen, das Schema sofort übernehmen: {ACCEPT_HINT}")


def _printable(text: str) -> str:
    """A folder name for display and storage: bytes that are no UTF-8 (surrogate escapes) as �."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _dump(rec: dict) -> str:
    # ASCII escapes: a path that is no valid UTF-8 survives the round trip through the database
    return json.dumps(rec, ensure_ascii=True)


def _record(proposal: Proposal, root: Path | str, when: datetime) -> dict:
    return {"pattern": proposal.pattern, "depth": proposal.depth, "detected_at": _iso(when),
            "projects": proposal.projects,
            "examples": [_printable(f"{e['number']} {e['name']}".strip()) for e in proposal.examples[:3]],
            "root": root_key(root)}


def store_detection(conn: sqlite3.Connection, proposal: Proposal, root: Path | str,
                    now: datetime | None = None) -> dict:
    """Accept a detection: it is used from now on (PROJECT_NUMBER_PATTERN=auto) and a pending
    different detection is forgotten. The caller commits."""
    when = now or _now()
    rec = _record(proposal, root, when)
    db.set_meta(conn, STORED_KEY, _dump(rec))
    db.set_meta(conn, CHECKED_KEY, _iso(when))
    conn.execute("DELETE FROM meta WHERE key IN (?, ?)", (DIFFERS_KEY, NONE_KEY))
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
        failed = _failed_detection(conn, settings.projects_dir)
        problem = (_failed_problem(failed) if failed else
                   "Projektnummern-Schema noch nicht erkannt (PROJECT_NUMBER_PATTERN=auto) – das geschieht beim "
                   "nächsten Indexierungslauf")
        return Layout(None, clamp_depth(configured_depth), True, configured_depth is None, problem=problem,
                      next_check=_next_check(failed) if failed else None)
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


def alternatives(pattern: str) -> frozenset[str]:
    """The top-level alternatives of an expression: '(?:A|B)' and 'B|A' → {A, B}; one scheme → {it}."""
    def split(p: str) -> list[str]:
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

    p = pattern.strip()
    while p.startswith("(?:") and p.endswith(")") and _wraps(p):
        p = p[3:-1]
    return frozenset(a.strip() for a in split(p))


def _wraps(p: str) -> bool:
    """Is ``p`` one group '(?:…)' from the first to the last character?"""
    depth, i, in_class = 0, 0, False
    while i < len(p):
        c = p[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0 and i != len(p) - 1:
                return False
        i += 1
    return True


def _same_detection(rec: dict, proposal: Proposal, result: Analysis, root: Path,
                    layout: Layout) -> tuple[bool, list[ProjectFolder] | None]:
    """Is a new detection the stored one? Compared by meaning, not as text: the same set of
    alternatives with the same depth – or exactly the same project folders found. Also returns what
    the stored scheme finds now, if that had to be scanned."""
    if alternatives(rec["pattern"]) == alternatives(proposal.pattern) and (
            not layout.depth_auto or proposal.depth == rec["depth"]):
        return True, None
    stored = scan_projects(root, rec["pattern"], layout.depth, unreadable=[], duplicates=[])
    new = result.projects if layout.depth_auto else scan_projects(root, proposal.pattern, layout.depth,
                                                                     unreadable=[], duplicates=[])
    return bool(stored) and {p.path for p in stored} == {p.path for p in new}, stored


def layout_for_index(conn: sqlite3.Connection, settings, *, now: datetime | None = None,
                     max_folders: int = MAX_FOLDERS) -> tuple[Layout, dict]:
    """The layout for an indexing pass. With PROJECT_NUMBER_PATTERN=auto this detects the scheme
    when nothing is stored yet and stores it; a stored one is checked again now and then, and a
    different result is only reported – the stored scheme stays in use until it is accepted
    (stability: a half-mounted share must never change which folders count as projects). A
    detection without a clear scheme is repeated at most every :data:`RECHECK_INTERVAL` (reading a
    large share costs directory traffic). Returns the layout and a small summary for the run stats
    (no warning there – the status page reads it)."""
    layout = current_layout(settings, conn)
    if not layout.auto:
        return layout, {}
    now = now or _now()
    root = Path(settings.projects_dir)
    info = {"pattern": layout.pattern, "depth": layout.depth, "source": layout.source}
    if problem := projects_root_problem(root):
        return (layout if layout.pattern else dataclasses.replace(layout, problem=problem)), info
    rec = stored_detection(conn, root)
    checked = _parse_time(db.get_meta(conn, CHECKED_KEY))
    recent = checked is not None and timedelta(0) <= now - checked < RECHECK_INTERVAL
    if rec is not None and layout.differs is None and recent:
        return layout, info
    if rec is None and recent and _failed_detection(conn, root) is not None:
        return layout, {**info, "detected": False}
    try:
        result = analyse(root, max_folders=max_folders)
    except Exception as exc:  # noqa: BLE001 – a detection problem must never stop the indexing
        log.exception("Projektnummern-Schema: Erkennung fehlgeschlagen")
        if rec is not None:
            return layout, info
        reason = f"Erkennung des Projektnummern-Schemas fehlgeschlagen ({type(exc).__name__}: {exc})."
        result = Analysis(root=str(root), max_depth=MAX_DEPTH, reason=reason)
    db.set_meta(conn, CHECKED_KEY, _iso(now))
    proposal = result.proposal
    if rec is None:
        if proposal is None:
            failed = {"root": root_key(root), "reason": _printable(result.reason or result.problem or ""),
                      "checked_at": _iso(now)}
            db.set_meta(conn, NONE_KEY, _dump(failed))
            conn.commit()
            log.warning("Projektnummern-Schema: %s", _failed_problem(failed))
            return current_layout(settings, conn), {**info, "detected": False}
        store_detection(conn, proposal, root, now)
        conn.commit()
        log.info("Projektnummern-Schema automatisch erkannt und gespeichert: %s, Tiefe %d (%d Projektordner, "
                 "z. B. %s)", proposal.pattern, proposal.depth, proposal.projects,
                 ", ".join(e["path"] for e in proposal.examples[:3]))
        layout = current_layout(settings, conn)
        return layout, {"pattern": layout.pattern, "depth": layout.depth, "source": layout.source,
                        "detected": True}
    if proposal is not None:
        same, stored = _same_detection(rec, proposal, result, root, layout)
        if same:
            if layout.differs is not None:
                conn.execute("DELETE FROM meta WHERE key = ?", (DIFFERS_KEY,))
                log.info("Projektnummern-Schema: die neue Erkennung stimmt wieder mit dem gespeicherten %s überein",
                         rec["pattern"])
        else:
            new = {**_record(proposal, root, now), "stored_projects": len(stored)}
            old = layout.differs or {}
            if (alternatives(old.get("pattern") or ""), old.get("depth")) != (alternatives(new["pattern"]),
                                                                                new["depth"]):
                log.warning("Projektnummern-Schema: eine neue Erkennung ergibt %s, Tiefe %d (%d Projektordner) – "
                            "weiter verwendet wird das gespeicherte %s, Tiefe %d (%d Projektordner). "
                            "Übernehmen mit: %s",
                            new["pattern"], new["depth"], new["projects"], rec["pattern"], layout.depth,
                            len(stored), ACCEPT_HINT)
            db.set_meta(conn, DIFFERS_KEY, _dump(new))
    # no clear scheme now (share half mounted, emptied?): keep everything as it is
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


def _hint_lines(h: dict, depth: int) -> list[str]:
    where = f" in {h['where']}" if h["where"] else ""
    count = ("etwa " if h["estimated"] else "") + de_count(h["folders"], "Ordner", "Ordner")
    line = (f"Weiteres Schema{where}: {h['pattern']}, {count} (z. B. {', '.join(h['examples'])})"
            + {"archive": " – unter einem Archivordner, ältere Nummerierung?",
               "side": " – Nebenablage (Angebote, Rechnungen …), meist keine Projekte"}.get(h["branch"], ""))
    setting = f"PROJECT_NUMBER_PATTERN={h['combined']}"
    if h["depth"] != depth:
        setting += f" und PROJECT_FOLDER_DEPTH={h['depth']}"
    return [line, f"  wenn das auch Projekte sind: {setting}"]


def _candidate_lines(candidates: list[dict]) -> list[str]:
    rows = []
    for c in candidates[:6]:
        where = f" in {c['where']}" if c["where"] else ""
        count = de_count(c["folders"], "Ordner", "Ordner")
        rows.append(f"  {c['pattern']} – Ebene {c['level']}{where}, {count} (z. B. {', '.join(c['examples'][:2])})"
                    + (f": {c['reason']}" if c.get("reason") else ""))
    return rows


def format_report(analysis: Analysis, current: dict | None = None, files: dict | None = None,
                  context: dict | None = None) -> str:
    """The German text report of ``python -m projektakte.structure``."""
    a, p = analysis, analysis.proposal
    out = [f"Projektordner: {a.root}"]
    if a.problem:
        return "\n".join(out + [f"FEHLER: {a.problem}"])
    out.append(f"Gelesen: {de_count(a.folders, 'Ordner', 'Ordner')} (nur Namen, bis Ebene {a.max_depth}) in "
               f"{a.seconds:.2f} s".replace(".", ",") + (" – Liste gekürzt" if a.truncated else ""))
    if a.unreadable:
        out.append(f"Nicht lesbar: {', '.join(a.unreadable[:5])}" + (" …" if len(a.unreadable) > 5 else ""))
    out.append("")
    if p is None:
        out.append("Kein eindeutiges Projektnummern-Schema gefunden.")
        out.append(f"  {a.reason}")
        if a.candidates:
            out.append("")
            out.append("Nächste Kandidaten:")
            out.extend(_candidate_lines(a.candidates))
        if a.levels:
            out.append("")
            out.append("Ordner je Ebene:")
            for lv in a.levels[:4]:
                out.append(f"  Ebene {lv['level']}: {de_count(lv['folders'], 'Ordner', 'Ordner')}, "
                           f"{de_int(lv['with_number'])} mit Nummer am Anfang (z. B. {', '.join(lv['examples'][:4])})")
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
        if p.outliers:
            out.append(f"Abweichende Nummern ({de_int(len(p.outliers))}) – zu selten für das Schema, werden nicht "
                       "gelesen: " + " · ".join(p.outliers[:8]) + (" …" if len(p.outliers) > 8 else ""))
        if p.duplicates:
            out.append(f"Doppelte Nummern ({len(p.duplicates)}) – gelesen wird nur der erste Ordner:")
            out.extend(f"  {d['number']}: {d['path']}  und  {d['duplicate']}" for d in p.duplicates[:MAX_LISTED])
        if p.deeper:
            out.append(f"Einzelne passende Ordner tiefer als Ebene {p.depth} (werden mit dieser Tiefe nicht erfasst): "
                       + " · ".join(p.deeper[:5]))
        if p.unreadable:
            out.append("Nicht lesbare Ordner beim Scan: " + ", ".join(p.unreadable[:5]))
        for hint in p.hints:
            out.extend(_hint_lines(hint, p.depth))
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
            out.append(f"  findet {de_count(n, 'Projektordner', 'Projektordner')}"
                       + (f" ({verdict})" if verdict else "") + (" – passt nicht zu diesem Ordner" if n == 0 else ""))
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
    out = [f"Dateien in {de_count(f['projects'], 'Projektordner', 'Projektordnern')}: "
           f"{de_int(f['files'])} ({de_size(f['bytes'])})"
           + (f" – nach {de_count(f['max_files'], 'Datei', 'Dateien')} abgebrochen" if f["truncated"] else "")]
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
        out.append(f"  … und {de_count(len(c['projects']) - MAX_LISTED, 'kleineres Projekt', 'kleinere Projekte')}")
    out.append("  passen ganz hinein: " + ", ".join(f"{label}: {de_int(s['fit'][label])} von {de_int(s['projects'])}"
                                                  for label in labels))
    if s["mean_document_tokens"]:
        per = s["documents_per_window"]
        out.append(f"  ein Dokument hat im Mittel {de_int(s['mean_document_tokens'])} Token (Median "
                   f"{de_int(s['median_document_tokens'])}) – es passen also etwa "
                   + ", ".join(f"{de_count(per[label], 'Dokument', 'Dokumente')} in {label}" for label in labels))
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


# argparse speaks English; its few messages that can reach the user are translated here
_ARGPARSE_DE = (
    (re.compile(r"^unrecognized arguments: "), "unbekannte Angaben: "),
    (re.compile(r"^the following arguments are required: "), "es fehlt: "),
    (re.compile(r"^argument (\S+): "), r"Option \1: "),
    (re.compile(r"expected one argument"), "erwartet einen Wert"),
    (re.compile(r"ignored explicit argument (.*)"), r"erwartet keinen Wert (\1)"),
    (re.compile(r"invalid (\w+) value: (.*)"), r"ungültiger Wert \2"),
    (re.compile(r"ambiguous option: (\S+) could match (.*)"), r"mehrdeutige Option \1 (passt zu \2)"),
)


class _HelpFormatter(argparse.HelpFormatter):
    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, "Aufruf: " if prefix is None else prefix)


class _Parser(argparse.ArgumentParser):
    """German usage and error messages; a wrong call exits with :data:`EXIT_USAGE`."""

    def error(self, message: str) -> None:  # type: ignore[override]
        for rx, repl in _ARGPARSE_DE:
            message = rx.sub(repl, message)
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: Fehler: {message}\n")


def _number_between(lo: int, hi: int | None = None):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"„{text}“ ist keine ganze Zahl") from None
        if value < lo or (hi is not None and value > hi):
            raise argparse.ArgumentTypeError(f"{value} liegt nicht zwischen {lo} und {hi}" if hi is not None
                                             else f"{value} ist kleiner als {lo}")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(
        prog="python -m projektakte.structure", add_help=False, formatter_class=_HelpFormatter,
        description="Ordnerstruktur des Projektordners prüfen: erkennt, wie Projektnummern aufgebaut sind und auf "
                    "welcher Ebene die Projektordner liegen, und schlägt PROJECT_NUMBER_PATTERN und "
                    "PROJECT_FOLDER_DEPTH vor. Liest nur Ordner- und Dateinamen, nie Inhalte, und schreibt nichts "
                    "(außer mit --uebernehmen).",
        epilog=f"Exit-Code: {EXIT_SCHEME} = Schema erkannt, {EXIT_NO_SCHEME} = kein eindeutiges Schema, "
               f"{EXIT_UNREADABLE} = Ordner nicht lesbar, {EXIT_USAGE} = falscher Aufruf. "
               "Mit PROJECT_NUMBER_PATTERN=auto merkt sich Projektakte das beim ersten Lauf erkannte Schema und "
               "wechselt danach nie von selbst; meldet die Statusseite eine abweichende Erkennung, übernimmt "
               "--uebernehmen das jetzt erkannte Schema (ab dem nächsten Indexierungslauf).")
    args = ap.add_argument_group("Argumente")
    args.add_argument("pfad", nargs="?", metavar="PFAD", help="Projektordner (Standard: PROJECTS_DIR)")
    opts = ap.add_argument_group("Optionen")
    opts.add_argument("-h", "--hilfe", "--help", action="help", help="diese Hilfe anzeigen")
    opts.add_argument("--tiefe", type=_number_between(1, MAX_DEPTH), default=MAX_DEPTH, metavar="N",
                      help=f"höchstens N Ebenen unter PFAD durchsuchen (1–{MAX_DEPTH}, Standard {MAX_DEPTH})")
    opts.add_argument("--json", action="store_true", help="Ergebnis als JSON ausgeben")
    opts.add_argument("--data-dir", metavar="DIR",
                      help="Datenverzeichnis mit der Datenbank (Standard: DATA_DIR) – zeigt die Textmenge je Projekt")
    opts.add_argument("--ohne-dateien", action="store_true", help="keine Dateistatistik (für sehr große Freigaben)")
    opts.add_argument("--max-dateien", type=_number_between(1), default=MAX_FILES, metavar="N",
                      help=f"Dateistatistik nach N Dateien abbrechen (Standard {de_int(MAX_FILES)})")
    opts.add_argument("--max-ordner", type=_number_between(1), default=MAX_FOLDERS, metavar="N",
                      help=f"höchstens N Ordner lesen (Standard {de_int(MAX_FOLDERS)})")
    opts.add_argument("--uebernehmen", action="store_true",
                      help="das jetzt erkannte Schema für PROJECT_NUMBER_PATTERN=auto speichern (ersetzt eine "
                           "frühere Erkennung)")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if args.data_dir:
        settings = dataclasses.replace(settings, data_dir=Path(args.data_dir))
    root = Path(args.pfad) if args.pfad else settings.projects_dir
    if args.uebernehmen and root_key(root) != root_key(settings.projects_dir):
        ap.error(f"--uebernehmen gilt nur für PROJECTS_DIR ({settings.projects_dir}) – PFAD weglassen")
    progress = _progress_printer(True)
    analysis = analyse(root, max_depth=args.tiefe, max_folders=args.max_ordner, progress=progress)
    files = context = current = None
    if not analysis.problem:
        current = current_check(root, settings)
        if analysis.proposal and not args.ohne_dateien:
            files = file_statistics(analysis.projects, root, max_files=args.max_dateien, progress=progress)
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
