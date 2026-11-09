"""Folder layout of PROJECTS_DIR: where the project folders sit and what their numbers look like.

Projektakte treats every folder whose name starts with a project number as a project (registry.py);
everything inside it is read, whatever its subfolders are called. This module

* analyses a project share read-only and bounded – folder names only, file contents are never
  read – and proposes PROJECT_NUMBER_PATTERN and PROJECT_FOLDER_DEPTH, verified with the scan the
  app itself uses (:func:`registry.scan_projects`). Optionally it counts the files per project
  (which types Projektakte reads and which not) and, given the database, the indexed text per
  project – does a project fit into a language model's context window?
* resolves the *effective* layout: the configured pattern, or with ``PROJECT_NUMBER_PATTERN=auto``
  the scheme a person confirmed – at exactly the depth it was detected at, which its evidence
  describes (a PROJECT_FOLDER_DEPTH set next to auto is not used, and the status page says so). A
  detection from folder names alone can be confidently wrong, so it is only ever a *proposal*
  (stored in the ``meta`` table with the evidence a person needs to judge it); nothing is read as a
  project until it is confirmed – on the status page („Übernehmen“) or with
  ``python -m projektakte.structure --uebernehmen`` – and a confirmed scheme never changes on its
  own: a later, different detection becomes a new proposal next to it, a rejected one is not
  offered again until the detection changes, and a share that is suddenly empty (not mounted)
  changes neither.
* is the command line tool ``python -m projektakte.structure`` (German output).

Names that are no valid UTF-8 (a share mounted without iocharset=utf8) are read with their real
path but shown, stored and printed with � (:func:`registry.printable`).

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
   such level whose folders are no containers. Folders with numbered internal subfolders are projects,
   and so are folders whose numbered subfolders are records of one kind (the same label in several
   folders, no folders of their own: PB-22-1001 Prüfbericht, G30001 Gutachten, 2024-017-001 Bauteil –
   a label that says "Projekt" is no kind of record).
   A container groups folders: folders that keep files of their own are containers only when (almost)
   all their subfolders belong to the deeper scheme.
5. *Outliers do not widen a scheme.* A digit count or separator variant is accepted only when at
   least :data:`VARIANT_MIN_COUNT` folders and :data:`VARIANT_MIN_SHARE` of the candidate use it;
   others stay exceptions and are listed. Numbers without letters need at least 4 digits, or a
   running number of at least 3 digits after a first group (2024-017, 24-017). Numbers must be unique.
   A scheme of bare digits that could also match a year (or year range, or date) excludes those with
   a guard (``(?!(?:19|20)\\d\\d(?!\\d))\\d{4}``), so year folders never become projects. A guard
   never drops projects silently: the folders it keeps out at the level of the projects are counted
   with the app's scan – a few are named, more mean the numbers reach the year range: the guard is
   left out, or, if year folders hold the projects, the scheme is refused. The index pass checks the
   guard of the effective scheme again (new folders 1900 … after the confirmation) and the status
   page names what it keeps out.
6. *One scheme.* Different schemes are never joined automatically. Schemes below an office folder
   (Angebote, Anfragen, Ausschreibungen, Rechnungen, Verwaltung, Buchhaltung, Akquise, Honorar…,
   Kalkulation, Vorlagen, Normen, Austausch – anywhere in the name: Honorarangebote, Ausgangsrechnungen)
   are never proposed; schemes below a customer register (Kunden …) or an archive folder (Archiv,
   Alt …) yield to a scheme elsewhere. The report names them as a hint with the alternation to set by
   hand. A folder with a project number of its own is never taken for such a folder.
7. *Confidence.* A scheme is proposed (in auto mode: offered for confirmation) only with at least
   :data:`MIN_PROJECTS` folders (below :data:`GOOD_PROJECTS` without any doubt), unique numbers (at
   most one or :data:`MAX_DUPLICATE_SHARE` duplicates; numbers that restart are no scheme at all), a
   real share of the folders next to them, and at least :data:`DOMINANCE` times as many folders as
   any competing scheme. Decisions use shares, not absolute sizes, so they do not change when the
   office grows; the inside of project folders is read from an evenly spread sample only, and project
   folders and the other folders next to them are counted in the same sample (a folder that was
   not opened is no exception). The depth is set by the levels that hold a real share of the
   projects; matches in office folders never raise it, matches in archive and customer folders do.
8. *Links* are followed inside the share like the app's scan does, but every real folder is listed
   once (links to ancestors, loops and second paths to a folder are skipped).
9. *Points to check.* What the names alone cannot decide is named with the proposal ("Bitte
   prüfen"), never decided silently: folders a guard keeps out, matches in office folders, names in a
   wrong encoding, most proposed folders carrying an office word in their own name (A-2024-001
   Angebot …), a further scheme in the same folders whose folders have far more structure of their
   own, and numbered subfolders that are several times as many as the proposed folders and look like
   projects themselves (1020 Stadt Aachen/1020-01 Kita …).
"""
from __future__ import annotations

import argparse
import dataclasses
import getpass
import hashlib
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
from .registry import (DATE_GUARD, ENCODING_HINT, YEAR_GUARD, ProjectFolder, alternatives, clamp_depth,
                       has_bad_encoding, loose_pattern, printable, project_head, projects_root_problem, scan_projects,
                       year_range_guard)

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
DEPTH_MIN_SHARE = 0.1  # a level with only this share of the matches does not raise the proposed depth
CONTAINER_SHARE = 0.5  # container: more than this (median) share of its subfolders belong to the deeper scheme …
CONTAINER_SHARE_WITH_FILES = 0.75  # … at least this for folders that keep files of their own (as projects do)
MAX_HINTS = 3  # further schemes named in the report
INNER_RATIO = 2.5  # numbered subfolders per proposed folder from which they may be the projects (a point to check)
MAX_FILES = 200_000  # files counted by the file statistics (all projects together)
MAX_LISTED = 15  # rows of the text report per list
CHARS_PER_TOKEN = 3.5  # German text, typical tokenizer of current models
CONTEXT_WINDOWS = (("32k", 32_000), ("128k", 128_000), ("1M", 1_000_000))
CONTEXT_RESERVE = 4_000  # tokens kept free for instructions, the question and the answer
# a pattern that never matches: the shape of project numbers for search pages while none is known
NO_MATCH = r"(?!)"
# PROJECT_NUMBER_PATTERN=auto: the share is analysed again at most this often (sooner when a person asks
# for it) – the result is a proposal next to the confirmed scheme, never a switch
RECHECK_INTERVAL = timedelta(hours=1)

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
# folders whose contents are no current projects (rule 6), named anywhere in the folder name (German
# compounds: Projektarchiv, Ausgangsrechnungen, Honorarangebote): older numberings …
_ARCHIVE_NAME = re.compile(r"archiv|(?<![a-zäöüß])(?:alt(?:e|akten|projekte|bestand)?|old)(?![a-zäöüß])",
                           re.IGNORECASE)
# … the office's own records with numberings of their own (offers, inquiries, tenders, invoices,
# templates, norms, exchange copies) …
_SIDE_NAME = re.compile(r"angebot|anfrage|ausschreibung|rechnung|verwaltung|buchhaltung|akquise|honorar|"
                        r"kalkulation|vorlage|normen|austausch", re.IGNORECASE)
# … and customer registers: projects may well be filed there, but a numbering found there yields to
# one found elsewhere (customer numbers next to project numbers)
_CUSTOMER_NAME = re.compile(r"kunde", re.IGNORECASE)
# words of the office's own records in the label of a numbered folder (A-2024-001 Angebot Kita): most of
# the proposed project folders carrying one is a point to check (offers instead of projects?)
_OFFICE_LABEL = re.compile(r"angebot|anfrage|ausschreibung|rechnung|honorar|kalkulation|akquise", re.IGNORECASE)
# a label that names projects is no kind of record (1020-001 Projekt, 2024-017 Bauvorhaben)
_PROJECT_LABEL = re.compile(r"(?:projekt|bauvorhaben|vorhaben|bv)(?![a-zäöüß])", re.IGNORECASE)
_BRANCH_ORDER = ("main", "customers", "archive")  # where projects are looked for, best first; never "side"
_BRANCH_TEXT = {"archive": "unter einem Archivordner – ältere Nummerierung?",
                "side": "in einer Nebenablage (Angebote, Anfragen, Rechnungen …) – meist keine Projekte",
                "customers": "in einem Kundenordner – Kundennummern?"}
_LABEL_START = re.compile(r"^[\s_\-–—:.,]+")
# German plural of the kinds of numbers that are no project numbers (rule 2)
_KIND_TEXT = {"Jahr": "Jahreszahlen", "Jahresbereich": "Jahresbereiche", "Monat": "Monatsangaben",
              "Datum": "Datumsangaben", "kurz": "kurze Gliederungsnummern"}
# guards against the date-like numbers a bare-digit scheme could match (rule 5; registry.is_guard knows them)
_YEAR_GUARD = YEAR_GUARD
_DATE_GUARD = DATE_GUARD
# what a guard excludes, for the explanation (singular, plural)
_GUARD_KIND = {"Jahr": ("Jahreszahl", "Jahreszahlen"), "Jahresbereich": ("Jahresbereich", "Jahresbereiche"),
               "Datum": ("Datum", "Datumsangaben")}


@dataclass(frozen=True)
class _Token:
    """The leading number of a folder name: ``BS-2024-0137 Schule`` → prefix BS, groups 2024, 0137."""
    prefix: str  # letters before the digits, upper case ("" if none)
    psep: str  # separator between prefix and digits ("" if none)
    groups: tuple[str, ...]
    seps: tuple[str, ...]  # separators between the groups
    glued: bool  # a letter follows the last group directly (no number boundary there)
    end: int = 0  # position after the last group in the name

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
    return _Token(prefix, psep, tuple(groups), tuple(seps), glued, pos)


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
    files: int = 0  # files directly in the folder (known once it is listed)
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
        """'archive', 'side' or 'customers' below an archive, office or customer folder (the topmost one
        decides), else 'main'."""
        if self._branch is None:
            parent = self.parent
            if parent is None or parent.depth == 0:
                self._branch = "main"
            elif parent.branch != "main":
                self._branch = parent.branch
            else:
                self._branch = _cabinet(parent)
        return self._branch

    @property
    def label(self) -> str:
        """The name after the leading number, normalised ('PB-22-1001 Prüfbericht' → 'prüfbericht')."""
        tok = self.token
        return _LABEL_START.sub("", self.name[tok.end:]).strip().casefold() if tok else ""


def _cabinet(node: _Node) -> str:
    """What a folder's name says about its contents: 'archive', 'side', 'customers' or 'main'. A
    folder with a project-like number of its own is a record, never a cabinet (2024-017 Angebot …)."""
    tok = node.token
    if tok is not None and _number_kind(tok.prefix, tok.groups) is None:
        return "main"
    name = node.name
    if _ARCHIVE_NAME.search(name):
        return "archive"
    if _SIDE_NAME.search(name):
        return "side"
    if _CUSTOMER_NAME.search(name):
        return "customers"
    return "main"


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
    files = 0
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
                    elif entry.is_file():
                        files += 1
                except OSError:
                    continue
    except OSError:
        if node is tree.root:
            raise
        node.unreadable, node.children = True, []
        tree.unreadable.append(node.rel)
        return
    entries.sort()
    node.files = files
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
                out.append((year_range_guard(_sep_regex(self.seps[0])), "kein Jahresbereich"))
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
        notes = [note for _, note in self.guards()]
        return self.text_plain + (f" ({', '.join(notes)})" if notes else "")

    @property
    def text_plain(self) -> str:
        """The scheme in words without its guards."""
        parts = []
        if self.prefix:
            parts.append(f"„{self.prefix}“")
            if sep := _sep_text(self.pseps):
                parts.append(sep)
        for i, (lo, hi) in enumerate(self.lengths):
            if i:
                parts.append(_sep_text(self.seps[i - 1]))
            parts.append(_digits_text(lo, hi))
        return ", ".join(parts)

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
def _record_series(cand: _Cand) -> bool:
    """Numbered records of one kind: most folders carry the same label after the number in several
    parent folders (PB-22-1001 Prüfbericht, G30001 Gutachten, 2024-017-001 Bauteil) and hold no
    folders of their own – project folders carry names of their own (2024-017 Kita Regenbogen) or
    their own structure (Pläne, Schriftverkehr), or a label that says so (1020-001 Projekt)."""
    labels = Counter(m.label for m in cand.members if m.label)
    if not labels:
        return False
    label, n = labels.most_common(1)[0]
    if _PROJECT_LABEL.match(label):  # "1020-001 Projekt": folders that say they are projects
        return False
    parents = {m.parent for m in cand.members if m.label == label}
    opened = [m for m in cand.members if m.children is not None and not m.unreadable]
    leaves = sum(1 for m in opened if not m.children)
    return (n >= VARIANT_MIN_COUNT and 2 * n >= len(cand.members) and len(parents) >= 2
            and bool(opened) and 2 * leaves >= len(opened))


def _container_of(outer: _Cand, inner: _Cand) -> bool:
    """Rule 4: are the folders of ``outer`` containers of ``inner``'s folders? Yes if most of them
    consist mostly of such folders (directly or in year folders). Folders with numbered internal
    subfolders are projects, and so are folders whose numbered subfolders are a series of records of
    one kind. A container groups folders: folders that mostly keep files of their own count as
    containers only when (almost) all their subfolders are such folders."""
    inside = set(inner.members)
    opened = [m for m in outer.members if m.children is not None and not m.unreadable]
    if not opened:
        return False
    if 2 * sum(1 for m in opened if any(_internal(c) for c in m.children or ())) >= len(opened):
        return False
    if _record_series(inner):
        return False
    shares = []
    for m in opened:
        kids = m.children or []
        bearing = sum(1 for c in kids if c in inside or (c.children and any(g in inside for g in c.children)))
        shares.append(bearing / len(kids) if kids else 0.0)
    share = statistics.median(shares)
    if 2 * sum(1 for m in opened if m.files) > len(opened):
        return share >= CONTAINER_SHARE_WITH_FILES
    return share > CONTAINER_SHARE  # mostly: more than half


def _branch(nodes: list[_Node]) -> str:
    """'archive', 'side' or 'customers' if most folders lie below such a folder, else 'main'."""
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
    # projects are looked for in the ordinary folders first, then in customer folders, then in archives
    branch = next((b for b in _BRANCH_ORDER if any(s.branch == b for s in schemes)), "main")
    above = _BRANCH_ORDER[:_BRANCH_ORDER.index(branch)]
    pool = [s for s in schemes if s.branch == branch]
    blocking = [c for c in unclear if unclear_branch[id(c)] in (*above, branch)]
    best, reason = (pool[0] if pool else None), None
    if best is not None and any(unclear_branch[id(c)] in above for c in blocking):
        c = max((c for c in blocking if unclear_branch[id(c)] in above), key=lambda c: len(c.items))
        where = ("gilt als ältere Nummerierung" if branch == "archive" else
                 "liegt nur in Kundenordnern")
        reason = (f"Kein Nummernschema ist eindeutig genug – {c.pattern} (Ebene {c.level}, "
                  f"{de_count(len(c.items), 'Ordner', 'Ordner')}{_where_text(c.items)}): {c.problem}. Das Schema "
                  f"{best.pattern}{_scheme_where(best)} {where} und wird nicht allein vorgeschlagen.")
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
                  "Angebote, Anfragen, Rechnungen und Vorlagen sind keine Projekte.")
    others = [s for s in schemes if s is not best]
    for s in schemes:
        verdict = "gewählt" if s is best else "Hinweis" if best is not None else "Kandidat"
        why = None
        if s is not best and best is not None:
            why = _BRANCH_TEXT.get(s.branch, f"seltener als {best.pattern}") if s.branch != best.branch else (
                f"seltener als {best.pattern}")
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

    @property
    def matched_est(self) -> float:
        """Project folders the share holds by estimate (sampling on the way down taken into account)."""
        return sum(n.weight for n, _ in self.matches)

    @property
    def exceptions_est(self) -> float:
        return sum(n.weight for n in self.exceptions)


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
    # a folder that was not opened (beyond the sample) may hold projects itself: it is no exception –
    # so matches and exceptions rest on the same sample and the share does not depend on the size
    exceptions = [n for n in others if n not in bearing and n.parent in bearing
                  and (n.children is not None or n.unreadable or n.depth >= max_depth)]
    return _Scan(matches, duplicates, exceptions)


def _proposed_depth(nodes: list[_Node], side: bool = False) -> int:
    """The deepest level that holds a real share of the projects – a single stray match far below
    (a copy in an exchange folder) does not make every scan look that deep, matches in office
    folders (offers, invoices, templates) never do (unless ``side``: the depth for a hint that names
    them), matches in archive and customer folders always count. (The caller raises 1 to the
    default 2 when that finds the same folders.)"""
    counted = nodes if side else [n for n in nodes if n.branch != "side"] or nodes
    depths = Counter(n.depth for n in counted)
    total = sum(depths.values())
    always = {n.depth for n in counted if n.branch != "main"}
    levels = [d for d, n in depths.items() if n >= DEPTH_MIN_SHARE * total or d in always]
    return max(levels or depths)


def _depth_for(root: _Node, head: re.Pattern, max_depth: int, side: bool = False) -> tuple[int, _Scan]:
    everything = _simulate(root, head, max_depth)
    if not everything.matches:
        return min(max_depth, DEFAULT_PROJECT_FOLDER_DEPTH), everything
    depth = _proposed_depth([n for n, _ in everything.matches], side)
    if depth < DEFAULT_PROJECT_FOLDER_DEPTH <= max_depth:
        # the default finds the very same folders today and also projects put into a year folder later
        # (the same folders – a copy in an exchange folder that sorts first is no "same" result)
        at_default = _simulate(root, head, DEFAULT_PROJECT_FOLDER_DEPTH)
        if {n for n, _ in at_default.matches} == {n for n, _ in _simulate(root, head, depth).matches}:
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
    guard: dict | None = None  # folders the scheme's guard excludes (kind, count, examples) or left it out
    notes: list[str] = field(default_factory=list)  # what a person should look at before confirming (German)
    inner: dict | None = None  # numbered subfolders inside the project folders (count, examples)


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
    bad_names: dict | None = None  # folder names that are no valid UTF-8: count, examples
    projects: list[ProjectFolder] = field(default_factory=list, repr=False)

    @property
    def exit_code(self) -> int:
        return EXIT_UNREADABLE if self.problem else EXIT_SCHEME if self.proposal else EXIT_NO_SCHEME

    def to_dict(self) -> dict:
        """Everything but the project list – names that are no valid UTF-8 shown with �."""
        data = dataclasses.asdict(self)
        data.pop("projects")
        return clean(data)


def clean(value):
    """``value`` (str, list, dict …) with every text made printable – for JSON, the database, a page."""
    if isinstance(value, str):
        return printable(value)
    if isinstance(value, dict):
        return {clean(k) if isinstance(k, str) else k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return value


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


def _unreadable_text(tree: _Tree) -> str:
    """A sentence about folders that could not be listed – they may hold the projects – or ''."""
    if not tree.unreadable:
        return ""
    names = ", ".join(tree.unreadable[:3]) + (" …" if len(tree.unreadable) > 3 else "")
    return (f"{de_count(len(tree.unreadable), 'Ordner war', 'Ordner waren')} nicht lesbar ({names}) – "
            "Berechtigungen des Lesekontos prüfen. ")


def _hint(tree: _Tree, best: _Scheme, other: _Scheme, max_depth: int, pattern: str | None = None) -> dict:
    """A further scheme with the alternation (and depth) to set by hand if it holds projects, too –
    ``pattern``: the proposed expression (the scheme's own unless its guard was left out)."""
    combined = f"(?:{pattern or best.pattern}|{other.pattern})"
    depth, _ = _depth_for(tree.root, project_head(combined), max_depth, side=True)
    own_depth, _ = _depth_for(tree.root, project_head(other.pattern), max_depth, side=True)
    parents = {m.parent for m in best.members}
    beside = sum(1 for m in other.members if m.parent in parents)
    return {"pattern": other.pattern, "description": other.text, "folders": round(other.est),
            "estimated": round(other.est) != len(other.members), "where": _where(other.members),
            "branch": other.branch, "examples": [n.name for n in other.members[:3]],
            "combined": combined, "depth": depth, "beside": 2 * beside >= len(other.members),
            "own_depth": own_depth, "structured": _structured_share(other.members)}


def _structured_share(nodes: list[_Node]) -> float | None:
    """The share of the opened folders that hold folders of their own (None: none was opened)."""
    opened = [n for n in nodes if n.children is not None and not n.unreadable]
    return sum(1 for n in opened if n.children) / len(opened) if opened else None


def _has_contents(path: Path) -> bool:
    """Does a folder hold anything (a folder or a file, hidden ones aside)? Unreadable: no."""
    try:
        with os.scandir(path) as entries:
            return any(not e.name.startswith(".") for e in entries)
    except OSError:
        return False


def _office_note(matches: list[_Node], hints: list[dict], depth: int, remedy: bool = True) -> str | None:
    """Rule 7, a point to check: most proposed project folders carry an office word in their own label
    (A-2024-001 Angebot Kita) – offers or invoices rather than projects? ``remedy``: name the settings
    for a further scheme in the same folders (unless another note does)."""
    office = [n for n in matches if _OFFICE_LABEL.search(n.label)]
    total = sum(n.weight for n in matches)
    share = sum(n.weight for n in office) / total if total else 0.0
    if share <= 0.5:
        return None
    note = (f"{round(100 * share)} % der vorgeschlagenen Projektordner tragen ein Wort der Büroablage (Angebot, "
            f"Anfrage, Rechnung, Honorar …) im Namen (z. B. {', '.join(n.rel for n in office[:3])}) – sind das "
            "Angebote, Anfragen oder Rechnungen statt Projekte?")
    if remedy and (h := next((h for h in hints if h["beside"] and not _OFFICE_LABEL.search(" ".join(h["examples"]))),
                             None)):
        note += (f" Sind die Ordner von {h['pattern']} (z. B. {', '.join(h['examples'][:2])}) die Projekte: "
                 f"{_alone_setting(h, depth)} von Hand einstellen.")
    return note


def _alone_setting(h: dict, depth: int) -> str:
    """The settings for a further scheme on its own (not combined with the proposed one)."""
    own = h.get("own_depth") or depth
    return f"PROJECT_NUMBER_PATTERN={h['pattern']} und PROJECT_FOLDER_DEPTH={own}"


def _structure_note(best: _Scheme, hints: list[dict], depth: int) -> str | None:
    """Rule 7, a point to check: a further scheme in the same folders as the proposed project folders
    whose folders mostly hold folders of their own (Pläne, Schriftverkehr …) while the proposed ones
    hardly do – the further scheme may be the projects."""
    mine = _structured_share(best.members)
    if mine is None:
        return None
    for h in hints:
        theirs = h.get("structured")
        if (h["beside"] and theirs is not None and theirs >= 0.5 and mine * 2 <= theirs
                and h["folders"] >= VARIANT_MIN_SHARE * best.est):
            return (f"Daneben, in denselben Ordnern, liegt das Schema {h['pattern']} "
                    f"({'etwa ' if h['estimated'] else ''}{de_count(h['folders'], 'Ordner', 'Ordner')}, z. B. "
                    f"{', '.join(h['examples'][:2])}); {round(100 * theirs)} % seiner Ordner haben eigene Unterordner, "
                    f"von den vorgeschlagenen Projektordnern nur {round(100 * mine)} %. Sind das Angebote oder "
                    f"eine Nebenablage, ist der Vorschlag richtig. Sind die Ordner von {h['pattern']} die Projekte, "
                    f"dann {_alone_setting(h, depth)} von Hand einstellen (beide zusammen: "
                    f"PROJECT_NUMBER_PATTERN={h['combined']}).")
    return None


def _inner_note(matches: list[_Node], root: Path, depth: int) -> str | None:
    """Rule 7, a point to check: the numbered subfolders inside the proposed project folders are several
    times (:data:`INNER_RATIO`) as many, carry names of their own – not one label per sub-number as an
    outline (2024-017_01 Pläne, _02 Schriftverkehr …) has, nor one label for all as records have – and
    keep folders or files of their own: they may be the projects, the proposed folders their customers
    (1020 Stadt Aachen/1020-01 Kita …). Reads at most :data:`SAMPLE_MEMBERS` of them (names only)."""
    kids = [(c, m) for m in matches for c in (m.children or ())
            if c.token is not None and _number_kind(c.token.prefix, c.token.groups) is None]
    proposed = sum(m.weight for m in matches)
    count = sum(c.weight for c, _ in kids)
    if not kids or count < INNER_RATIO * proposed:
        return None
    labels: dict[tuple | None, list[str]] = {}  # by sub-number (1020-01 → 01), None: an own numbering
    for c, m in kids:
        pt, t = m.token, c.token
        n = len(pt.groups) if pt is not None else 0
        sub = t.groups[n:] if pt is not None and t.prefix == pt.prefix and t.groups[:n] == pt.groups else None
        labels.setdefault(sub or None, []).append(c.label)
    judged = [v for k, v in labels.items() if len(v) >= 2 or k is None]
    if sum(len(v) for v in judged) < MIN_PROJECTS:
        return None
    fixed = sum(Counter(v).most_common(1)[0][1] for v in judged)
    if 2 * fixed >= sum(len(v) for v in judged):
        return None  # an outline (same label for the same sub-number) or records of one kind

    def kept(c: _Node) -> bool:  # a folder or file of its own – listed already, or looked at now
        return (bool(c.children) or c.files > 0) if c.children is not None else _has_contents(root / c.rel)

    sample = _sample([c for c, _ in kids], SAMPLE_MEMBERS)
    if 2 * sum(1 for c in sample if kept(c)) < len(sample):
        return None
    # the expression their numbers have (the most common shape) and the depth they lie at – a starting
    # point to set by hand (it is no proposal: such numbers may be too short to be told from others)
    shapes = Counter((c.token.prefix, len(c.token.groups)) for c, _ in kids)
    (prefix, k), _ = shapes.most_common(1)[0]
    items = [(c, c.token) for c, _ in kids if (c.token.prefix, len(c.token.groups)) == (prefix, k)]
    expression = _build(items[0][0].depth, prefix, k, items).pattern
    level = max(c.depth for c, _ in kids)
    return (f"Die vorgeschlagenen Ordner enthalten je mehrere nummerierte Unterordner mit eigener Ablage (etwa "
            f"{de_int(count)} bei {de_count(proposed, 'Projektordner', 'Projektordnern')}, z. B. "
            f"{', '.join(c.rel for c, _ in kids[:3])}). Sind das Teile eines Projekts (Gutachten, Bauabschnitte, "
            f"Teilaufträge), ist der Vorschlag richtig. Sind es eigene Projekte (z. B. Kunde → Projekt), dann "
            f"PROJECT_NUMBER_PATTERN passend zu ihren Nummern (etwa {expression}) und PROJECT_FOLDER_DEPTH="
            f"{max(level, depth)} von Hand einstellen und mit python -m projektakte.structure prüfen.")


def _reject(result: Analysis, reason: str, started: float) -> Analysis:
    result.reason = reason
    for info in result.candidates:
        if info["verdict"] == "gewählt":
            info["verdict"], info["reason"] = "Kandidat", "zu unsicher (siehe oben)"
    result.seconds = round(time.monotonic() - started, 2)
    return result


# --- guards (rule 5): a guard must never drop projects silently
# a year range with any separator, a full date and a bare year – checked in this order, so that a
# range (2019-1900) is not named a year
_KIND_PROBES = (("Jahresbereich", re.compile(r"(?:19|20)\d\d[-_.\s]?(?:19|20)\d\d(?!\d)")),
                ("Datum", re.compile(r"(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)")),
                ("Jahr", re.compile(r"(?:19|20)\d\d(?!\d)")))
MAX_GUARD_EXAMPLES = 8


def guard_kind(names: Iterable[str]) -> tuple[str, str]:
    """(singular, plural) of what a guard keeps out of these folder names: 'Jahreszahl' …"""
    kinds = Counter(next((kind for kind, rx in _KIND_PROBES if rx.match(name)), "") for name in names)
    kind = kinds.most_common(1)[0][0] if kinds else ""
    return _GUARD_KIND.get(kind, ("Jahreszahl oder Datum", "Jahreszahlen oder Datumsangaben"))


_KIND_ARTICLE = {"Jahreszahl": ("eine Jahreszahl", "Jahreszahlen"), "Jahresbereich": ("ein Jahresbereich", "Jahresbereichen"),
                 "Datum": ("ein Datum", "Datumsangaben")}


def guard_text(count: int, examples: list[str], kind: str, dropped: bool = False) -> str:
    """German line about the folders a guard excludes (or would exclude, had it not been left out)."""
    shown = ", ".join(examples[:5]) + (" …" if count > min(5, len(examples)) else "")
    if dropped:
        one, many = _KIND_ARTICLE.get(kind, ("eine Jahreszahl oder ein Datum", "Jahreszahlen oder Datumsangaben"))
        return (f"Ohne Ausschluss von {many}: {de_count(count, 'Ordner', 'Ordner')} neben den Projektordnern "
                f"beginnen mit einer Nummer, die auch {one} sein könnte, und werden als Projekte gelesen "
                f"({shown}).")
    return f"{de_count(count, 'Ordner würde', 'Ordner würden')} als {kind} ausgeschlossen und nicht gelesen: {shown}"


@dataclass
class _GuardCheck:
    pattern: str  # the expression to use: with the guard, or without it when it would drop projects
    info: dict | None = None  # kind, count, examples, dropped
    refusal: str | None = None
    loose: str | None = None  # the expression without its guard (None: it has none)
    clash: list[str] = field(default_factory=list)  # folders holding projects the loose expression matches


def _guard_refusal(pattern: str, count: int, examples: list[str], clash: list[str]) -> str:
    singular, plural = guard_kind(Path(e).name for e in examples)
    return (f"Projektnummern und {plural} sind nicht zu unterscheiden: {de_count(count, 'Ordner beginnt', 'Ordner beginnen')} "
            f"mit einer Nummer, die nach dem Schema {pattern} als {singular} gilt und nicht gelesen würde (z. B. "
            f"{', '.join(examples[:3])}); ohne diesen Ausschluss würden aber die Ordner {', '.join(clash[:3])}, in "
            "denen die Projektordner liegen, selbst als Projekte gelesen.")


def _check_guard(tree: _Tree, best: _Scheme) -> _GuardCheck:
    """Rule 5: a guard keeps years (dates, year ranges) out of a bare-digit scheme. The folders it
    excludes at the level of the projects – next to project folders, holding none – are counted.
    A few are named in the proposal; more are projects whose numbers reach the year range: then the
    guard is left out, unless the folders holding the projects (year folders) would match without
    it – then the scheme cannot tell projects from years and is refused."""
    pattern = best.pattern
    loose = loose_pattern(pattern)
    if loose is None:
        return _GuardCheck(pattern)
    head, loose_head = project_head(pattern), project_head(loose)
    members = set(best.members)
    holding = {a for m in members for a in m.ancestors() if a.depth}
    clash = [a.rel for a in sorted((a for a in holding if loose_head.match(a.name)), key=lambda a: a.rel)]
    # (the candidate's folders were grouped by their digits – the guard may exclude some of them too)
    excluded = [n for level in sorted({c.level for c in best.cands}) for n in tree.level(level)
                if n not in holding and not any(a in members for a in n.ancestors())
                and loose_head.match(n.name) and not head.match(n.name)]
    if not excluded:
        return _GuardCheck(pattern, loose=loose, clash=clash)
    count = round(sum(n.weight for n in excluded))
    examples = [n.rel for n in excluded[:MAX_GUARD_EXAMPLES]]
    info = _guard_info(count, examples, dropped=False)
    if not _guard_loses(count, best.est):
        return _GuardCheck(pattern, info, loose=loose, clash=clash)
    if clash:
        return _GuardCheck(pattern, info, _guard_refusal(pattern, count, examples, clash), loose, clash)
    info["dropped"] = True
    return _GuardCheck(loose, info, loose=loose, clash=clash)


def _guard_info(count: int, examples: list[str], *, dropped: bool) -> dict:
    singular, plural = guard_kind(Path(e).name for e in examples)
    return {"kind": singular, "kinds": plural, "count": count, "examples": examples[:MAX_GUARD_EXAMPLES],
            "dropped": dropped}


def _guard_loses(count: float, projects: float) -> bool:
    """Would a guard keep out more than a few stray folders (2, or YEAR_LOSS_SHARE of the projects)?"""
    return count > max(2, YEAR_LOSS_SHARE * projects)


@dataclass
class _Verified:
    """A scheme checked against the listed tree and the app's own scan."""
    depth: int
    everything: _Scan
    final: _Scan
    found: list[ProjectFolder]
    unreadable: list[Path]
    duplicates: list[tuple[str, Path, Path]]
    excluded: list[tuple[str, Path]]  # folders its guard keeps out next to project folders (whole share)


def _verify(tree: _Tree, root: Path, best: _Scheme, pattern: str, max_depth: int) -> _Verified | str:
    """Depth, clash with the folders holding the projects, share of the project folders among the
    folders next to them, and the app's own scan (rule 7) – or the German reason why not."""
    head = project_head(pattern)
    depth, everything = _depth_for(tree.root, head, max_depth)
    holding = {a for m in best.members for a in m.ancestors()}
    if clash := [n for n, _ in everything.matches if n in holding]:
        return (f"Das Schema {pattern} passt auch auf die Ordner, in denen die Projektordner liegen "
                f"({', '.join(n.rel for n in clash[:3])}) – diese würden selbst als Projekte gelesen.")
    final = _simulate(tree.root, head, depth)
    # the share of project folders next to them, both counted in the same sample
    n_est, ex_est = final.matched_est, final.exceptions_est
    need = MIN_SHARE_FEW if n_est < GOOD_PROJECTS else MIN_SHARE
    if n_est + ex_est and n_est < need * (n_est + ex_est):
        return (f"Nur {round(100 * n_est / (n_est + ex_est))} % der Ordner neben den Projektordnern passen zum Schema "
                f"{pattern} ({_passing_text(n_est, len(final.matches), 'passender', 'passende')}, "
                f"{_passing_text(ex_est, len(final.exceptions), 'anderer', 'andere')}) – zu wenig für ein sicheres "
                "Schema.")
    unreadable: list[Path] = []
    duplicates: list[tuple[str, Path, Path]] = []
    excluded: list[tuple[str, Path]] = []
    found = scan_projects(root, pattern, depth, unreadable=unreadable, duplicates=duplicates, excluded=excluded)
    # the same folder reached through a link is no second project (the scan skips it as well)
    duplicates = [d for d in duplicates if os.path.realpath(d[1]) != os.path.realpath(d[2])]
    return _Verified(depth, everything, final, found, unreadable, duplicates, excluded)


def _inner_numbering(matches: list[_Node]) -> dict | None:
    """Numbered subfolders inside the (opened) project folders – no dates or short outline numbers:
    a person sees at once whether 1020 Stadt Aachen holds 1020-01 Kita (projects) or 01 Pläne."""
    kids = [c for m in matches for c in (m.children or ())
            if c.token is not None and _number_kind(c.token.prefix, c.token.groups) is None]
    if not kids:
        return None
    examples: dict[str, str] = {}  # one example per label: 2024-017_01 Pläne once, 1020-01 Kita, 1020-02 Schule …
    for c in kids:
        if len(examples) < 6:
            examples.setdefault(c.label or c.name, c.rel)
    return {"count": round(sum(c.weight for c in kids)), "examples": list(examples.values()),
            "internal": round(sum(c.weight for c in kids if _internal(c)))}


def _passing_text(n: float, listed: int, singular: str, plural: str) -> str:
    return de_count(n, singular, plural) if round(n) == listed else f"etwa {de_count(n, singular, plural)}"


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
    bad = [n for n in tree.nodes if has_bad_encoding(n.name)]
    if bad:
        result.bad_names = {"count": len(bad), "examples": [n.rel for n in bad[:MAX_GUARD_EXAMPLES]]}
    choice = _choose(tree)
    result.candidates = choice.candidates[:12]
    result.rejected = [{"pattern": i["pattern"], "reason": i["reason"]} for i in choice.candidates
                       if i["verdict"] in ("abgelehnt", "Sammelordner", "in Projektordnern")][:8]
    best = choice.best
    if best is None:
        return _reject(result, _unreadable_text(tree) + _no_scheme_reason(tree, choice), started)
    guard = _check_guard(tree, best)
    if guard.refusal:
        return _reject(result, guard.refusal, started)
    pattern = guard.pattern
    v = _verify(tree, root, best, pattern, max_depth)
    if isinstance(v, str):
        return _reject(result, v, started)
    if guard.loose and pattern != guard.loose:
        # the app's scan of the whole share decides what the guard keeps out, not the sample (rule 5)
        guard.info = None
        if v.excluded:
            examples = [_rel(folder, root) for _, folder in v.excluded[:MAX_GUARD_EXAMPLES]]
            guard.info = _guard_info(len(v.excluded), examples, dropped=False)
            if _guard_loses(len(v.excluded), len(v.found) + len(v.excluded)):
                if guard.clash:
                    return _reject(result, _guard_refusal(pattern, len(v.excluded), examples, guard.clash), started)
                pattern, guard.info["dropped"] = guard.loose, True
                v = _verify(tree, root, best, pattern, max_depth)
                if isinstance(v, str):
                    return _reject(result, v, started)
    elif guard.info and guard.info["dropped"]:  # the exact folders the guard would have kept out
        guarded = project_head(best.pattern)
        lost = [f for f in v.found if not guarded.match(f.path.name)]
        guard.info = _guard_info(len(lost), [_rel(f.path, root) for f in lost], dropped=True)
    depth, everything, final, found = v.depth, v.everything, v.final, v.found
    unreadable, duplicates = v.unreadable, v.duplicates
    if len(found) < MIN_PROJECTS:  # the share changed while it was read
        return _reject(result, f"Das Schema {pattern} fand beim Gegenprüfen nur "
                               f"{de_count(len(found), 'Projektordner', 'Projektordner')} – Freigabe vollständig "
                               "eingebunden?", started)
    if len(duplicates) > max(1, MAX_DUPLICATE_SHARE * len(found)):
        examples = "; ".join(f"{_rel(a, root)} und {_rel(b, root)}" for _, a, b in duplicates[:2])
        return _reject(result, f"Viele doppelte Nummern mit dem Schema {pattern} ({len(duplicates)} bei "
                               f"{de_count(len(found), 'Projektordner', 'Projektordnern')}, z. B. {examples}) – "
                               "gelesen würde jeweils nur der erste Ordner. Nummern eindeutig machen oder "
                               "PROJECT_NUMBER_PATTERN von Hand einstellen.", started)
    doubts = [text for text, present in (("doppelte Nummern", duplicates), ("abweichende Nummern", best.outliers),
                                         ("ein weiteres Nummernschema", choice.others)) if present]
    if len(found) < GOOD_PROJECTS and doubts:
        return _reject(result, f"Nur {de_count(len(found), 'Projektordner', 'Projektordner')} mit dem Schema "
                               f"{pattern}, und daneben gibt es {' und '.join(doubts)} – zu wenig für ein "
                               "sicheres Schema.", started)
    # what a person should look at before confirming
    notes: list[str] = []
    if guard.info:
        notes.append(guard_text(guard.info["count"], guard.info["examples"], guard.info["kind"],
                                guard.info["dropped"]))
    side = [n for n, _ in final.matches if n.branch == "side"]
    if side:
        n_side = round(sum(n.weight for n in side))
        notes.append(f"{de_count(n_side, 'gefundener Ordner liegt', 'gefundene Ordner liegen')} in einer Nebenablage "
                     f"(Angebote, Anfragen, Rechnungen …) und {'wird' if n_side == 1 else 'werden'} als "
                     f"Projekt{'' if n_side == 1 else 'e'} gelesen: {', '.join(n.rel for n in side[:3])}"
                     + (" …" if len(side) > 3 else ""))
    if result.bad_names:
        bad_names = result.bad_names
        notes.append(f"{de_count(bad_names['count'], 'Ordnername hat', 'Ordnernamen haben')} eine falsche "
                     f"Zeichenkodierung (z. B. {', '.join(bad_names['examples'][:3])}) – {ENCODING_HINT}.")
    if tree.unreadable:
        notes.append(_unreadable_text(tree).strip())
    parents = Counter(_rel(p.path.parent, root) for p in found if p.path.parent != root)
    hints = [_hint(tree, best, other, max_depth, pattern) for other in choice.others[:MAX_HINTS]]
    matched = [n for n, _ in final.matches]
    structured = _structure_note(best, hints, depth)
    notes += [note for note in (_office_note(matched, hints, depth, remedy=not structured), structured,
                                _inner_note(matched, root, depth)) if note]
    result.projects = found
    result.alternatives = hints
    result.proposal = Proposal(
        pattern=pattern, depth=depth, projects=len(found),
        description=best.cands[0].text_plain if guard.info and guard.info["dropped"] else best.text,
        examples=[{"number": p.number, "name": p.name, "path": _rel(p.path, root)} for p in found[:MAX_LISTED]],
        levels=dict(sorted(Counter(len(_rel(p.path, root).split("/")) for p in found).items())),
        parents=[[k, v] for k, v in parents.most_common(8)],
        exceptions=[n.rel for n in final.exceptions[:MAX_LISTED * 2]], exceptions_total=len(final.exceptions),
        duplicates=[{"number": num, "path": _rel(a, root), "duplicate": _rel(b, root)}
                    for num, a, b in duplicates[:MAX_LISTED]],
        deeper=[node.rel for node, _ in everything.matches if node.depth > depth][:MAX_LISTED],
        second_scheme=hints[0] if hints else None,
        unreadable=[_rel(p, root) for p in unreadable[:MAX_LISTED]],
        outliers=[o.rel for o in best.outliers[:MAX_LISTED]], hints=hints, guard=guard.info, notes=notes,
        inner=_inner_numbering([n for n, _ in final.matches]))
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
        row = {"number": project.number, "files": 0, "bytes": 0, "readable": 0, "too_big": 0, "bad_names": 0}
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
                    if has_bad_encoding(os.path.relpath(entry.path, project.path)):
                        row["bad_names"] += 1  # cannot be stored – the documents pass skips it
                    elif size <= MAX_FILE_BYTES:
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
            "bad_names": sum(r["bad_names"] for r in per_project),
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
# PROJECT_NUMBER_PATTERN=auto keeps its state in the meta table, every record for one PROJECTS_DIR. A
# detection is only ever a proposal: it is used once a person confirmed it (status page or
# --uebernehmen), and a confirmed scheme is never replaced on its own – a later, different detection
# becomes a new proposal next to it (a half-mounted share must never change which folders count).
CONFIRMED_KEY = "project_layout_confirmed"  # the scheme a person confirmed – the only one ever used
PROPOSAL_KEY = "project_layout_proposal"  # the latest detection with its evidence, waiting for a person
REJECTED_KEY = "project_layout_rejected"  # the proposal rejected last (not offered again while it stays)
CHECK_KEY = "project_layout_check"  # the latest detection: time, outcome, reason, candidates, further schemes
NOTES_KEY = "project_layout_notes"  # what the latest project scan noticed (guard, names in a wrong encoding)
# records of the development state before the confirmation existed – stored without anyone confirming
LEGACY_KEYS = ("project_layout_auto", "project_layout_auto_differs", "project_layout_auto_checked",
               "project_layout_auto_none")
PENDING_TEXT = "Vorschlag zur Projektstruktur wartet auf Bestätigung"
ANONYMOUS = "unbekannt (ohne Anmeldung)"  # who decided while the access control is switched off
MAX_EXAMPLES = 10  # example project folders kept with a proposal


class ProposalError(Exception):
    """A confirmation or rejection that does not match the current proposal (German message)."""


@dataclass(frozen=True)
class Layout:
    """Which folders count as projects: the effective PROJECT_NUMBER_PATTERN and folder depth. With
    PROJECT_NUMBER_PATTERN=auto only a confirmed scheme is effective; a proposal waits for a person."""
    pattern: str | None  # None: no usable scheme – the project sync is skipped
    depth: int
    auto: bool  # PROJECT_NUMBER_PATTERN=auto
    depth_auto: bool  # the depth comes from the detection (PROJECT_FOLDER_DEPTH unset or auto)
    problem: str | None = None  # German: why there is no usable scheme
    confirmed: dict | None = None  # auto mode: the confirmed scheme with the evidence it was confirmed on
    proposal: dict | None = None  # auto mode: a detection waiting for a person – never used before that
    rejected: dict | None = None  # auto mode: the proposal rejected last
    check: dict | None = None  # auto mode: the latest detection (time, outcome, candidates, further schemes)
    notes: dict | None = None  # what the latest project scan with this scheme noticed
    next_check: str | None = None  # auto mode without a clear scheme: the next detection (UTC, ISO)
    # auto mode: a PROJECT_FOLDER_DEPTH that is set but not used – the depth is the one the confirmed
    # proposal was detected (and its evidence computed) at, never another one
    ignored_depth: int | None = None

    @property
    def state(self) -> str:
        """configured | confirmed | pending | rejected | none (no clear scheme) | unchecked"""
        if not self.auto:
            return "configured"
        if self.confirmed:
            return "confirmed"
        if self.proposal:
            return "pending"
        if self.rejected:
            return "rejected"
        return "none" if self.check and self.check.get("outcome") != "proposal" else "unchecked"

    @property
    def unclear(self) -> bool:
        """Auto mode with neither a confirmed scheme nor a proposal, and the latest detection found no
        clear scheme – also after a rejection, so its reason and candidates are shown."""
        return (self.auto and not self.confirmed and not self.proposal and bool(self.check)
                and self.check.get("outcome") != "proposal")

    @property
    def detected_at(self) -> str | None:
        return (self.confirmed or {}).get("detected_at")

    @property
    def examples(self) -> tuple[str, ...]:
        return tuple((self.confirmed or {}).get("examples") or ())[:3]

    @property
    def projects(self) -> int | None:
        return (self.confirmed or {}).get("projects")

    @property
    def source(self) -> str:
        if not self.auto:
            return "eingestellt"
        if self.confirmed:
            return (f"bestätigt am {local_time_text(self.confirmed.get('confirmed_at'))} von "
                    f"{self.confirmed.get('confirmed_by') or 'unbekannt'}")
        return "automatisch – noch nicht bestätigt"

    @property
    def warning(self) -> str | None:
        """A newer proposal next to the confirmed scheme – which stays in use until it is confirmed."""
        p = self.proposal
        if not p or not self.pattern:
            return None
        examples = ", ".join((p.get("examples") or [])[:3])
        now = p.get("current_projects")
        return (f"Neuer Vorschlag zur Projektstruktur ({local_time_text(p.get('detected_at'))}): {p['pattern']}, "
                f"Tiefe {p['depth']} ({p.get('projects')} Projektordner{', z. B. ' + examples if examples else ''}). "
                f"Weiter verwendet wird das bestätigte {self.pattern}, Tiefe {self.depth}"
                f"{f' (findet derzeit {now} Projektordner)' if now is not None else ''}. Übernehmen auf der "
                f"Statusseite oder mit: {ACCEPT_HINT}")

    @property
    def depth_notice(self) -> str | None:
        """Auto mode with PROJECT_FOLDER_DEPTH set to another depth than the (proposed) scheme's: it is
        not used – said on the status page and at start-up, never applied silently."""
        rec = self.confirmed or self.proposal
        if self.ignored_depth is None or rec is None or self.ignored_depth == rec.get("depth"):
            return None
        which = ("des bestätigten Vorschlags" if self.confirmed else "des Vorschlags (nach dem Übernehmen)")
        return (f"PROJECT_FOLDER_DEPTH={self.ignored_depth} ist eingestellt, gilt aber nicht zusammen mit "
                f"PROJECT_NUMBER_PATTERN=auto: gelesen wird die Ordnertiefe {which}, {rec.get('depth')} – genau "
                "die, auf der die Erkennung und ihre Belege beruhen. PROJECT_FOLDER_DEPTH aus projektakte.env "
                "entfernen (oder auf auto setzen); soll eine andere Tiefe gelten, PROJECT_NUMBER_PATTERN und "
                "PROJECT_FOLDER_DEPTH beide fest einstellen (Vorschlag: python -m projektakte.structure).")

    @property
    def notices(self) -> list[str]:
        """Warnings of the latest project scan with this scheme (guard, names in a wrong encoding), a
        PROJECT_FOLDER_DEPTH that auto mode does not use and, next to a confirmed scheme, a newer
        detection that found no clear scheme at all."""
        out = scan_notices(self.notes)
        if notice := self.depth_notice:
            out.insert(0, notice)
        c = self.check
        if self.confirmed and c and c.get("outcome") != "proposal" and c.get("checked_at"):
            out.append(f"Die neueste Erkennung ({local_time_text(c.get('checked_at'))}) ergibt kein eindeutiges "
                       f"Schema: {c.get('reason')} Weiter verwendet wird das bestätigte {self.pattern}.")
        return out

    def summary(self) -> str:
        """One German line for the start-up log."""
        if self.pattern is None:
            if not self.auto:
                return "unbrauchbar – Projekte werden nicht abgeglichen"
            if self.proposal:
                return f"{PENDING_TEXT} ({self.proposal['pattern']}, Tiefe {self.proposal['depth']})"
            if self.rejected:
                return f"Vorschlag {self.rejected['pattern']} abgelehnt – noch kein Schema bestätigt"
            when = (f"nächste Prüfung frühestens {local_time_text(self.next_check)}" if self.next_check
                    else "beim nächsten Indexierungslauf")
            if self.state == "none":
                return f"automatisch – kein eindeutiges Schema erkannt ({when})"
            return f"automatisch – noch nicht erkannt ({when})"
        depth = f"Tiefe {self.depth}" + (" (automatisch)" if self.auto and self.depth_auto else "")
        return f"{self.pattern} ({self.source}), {depth}"

    def to_dict(self) -> dict:
        """For the status page and /api/status – the stored records without the absolute PROJECTS_DIR
        they are kept for (``root``): the path of the share is no business of the API."""
        def public(rec: dict | None) -> dict | None:
            return {k: v for k, v in rec.items() if k != "root"} if rec else rec

        return clean({
            "pattern": self.pattern, "depth": self.depth, "auto": self.auto, "depth_auto": self.depth_auto,
            "state": self.state, "unclear": self.unclear, "source": self.source, "depth_text": depth_text(self.depth),
            "detected_at": self.detected_at, "examples": list(self.examples), "projects_at_detection": self.projects,
            "problem": self.problem, "warning": self.warning, "notices": self.notices,
            "ignored_depth": self.ignored_depth, "next_check": self.next_check, "confirmed": public(self.confirmed),
            "proposal": public(self.proposal), "rejected": public(self.rejected), "check": public(self.check),
            "accept_hint": ACCEPT_HINT})


def scan_notices(notes: dict | None) -> list[str]:
    """German warnings from what a project scan noticed (see :func:`record_scan_notes`)."""
    if not notes:
        return []
    out = []
    if g := notes.get("guard"):
        kind = guard_kind(Path(e).name for e in g.get("examples") or ())[0]
        out.append(f"{guard_text(g['count'], g.get('examples') or [], kind)} – obwohl sie neben Projektordnern "
                   f"liegen. Sind das Projekte: PROJECT_NUMBER_PATTERN ohne den Ausschluss von Hand einstellen "
                   f"(python -m projektakte.structure zeigt die Ordner) oder die Ordner umbenennen.")
    if b := notes.get("bad_names"):
        out.append(f"{de_count(b['count'], 'Ordnername hat', 'Ordnernamen haben')} eine falsche Zeichenkodierung "
                   f"(z. B. {', '.join((b.get('examples') or [])[:3])}) – sie werden mit � angezeigt. "
                   f"{ENCODING_HINT}.")
    if f := notes.get("bad_files"):
        one = f["count"] == 1
        examples = ", ".join((f.get("examples") or [])[:3])
        out.append(f"{de_count(f['count'], 'Datei in Projektordnern wird', 'Dateien in Projektordnern werden')} nicht "
                   f"gelesen, weil {'ihr Name' if one else 'ihre Namen'} eine falsche Zeichenkodierung "
                   f"{'hat' if one else 'haben'} (z. B. {examples}) – {ENCODING_HINT}, dann "
                   f"{'wird sie' if one else 'werden sie'} beim nächsten Indexierungslauf gelesen.")
    return out


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


def _for_root(conn: sqlite3.Connection | None, key: str, root: Path | str) -> dict | None:
    """A stored record for this PROJECTS_DIR (one made for another folder – the demo data set before
    the switch to the real share – does not count)."""
    rec = _load(conn, key)
    return rec if rec is not None and rec.get("root") == root_key(root) else None


def _valid_scheme(rec: dict | None) -> bool:
    return (isinstance(rec, dict) and isinstance(rec.get("pattern"), str) and not pattern_problem(rec["pattern"])
            and isinstance(rec.get("depth"), int) and 1 <= rec["depth"] <= MAX_DEPTH)


def _dump(rec: dict) -> str:
    # printable names and ASCII escapes: the database only ever holds valid text
    return json.dumps(clean(rec), ensure_ascii=True)


def _save(conn: sqlite3.Connection, key: str, rec: dict) -> None:
    db.set_meta(conn, key, _dump(rec))


def _delete(conn: sqlite3.Connection, *keys: str) -> None:
    conn.execute(f"DELETE FROM meta WHERE key IN ({', '.join('?' * len(keys))})", keys)


def proposal_id(root: Path | str, pattern: str, depth: int) -> str:
    """Identifies a proposal by meaning (folder, set of alternatives, depth): the same detection made
    again keeps its id, so a confirmation refers to what the person saw."""
    text = "\0".join([root_key(root), *sorted(alternatives(pattern)), str(depth)])
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def proposal_record(analysis: Analysis, root: Path | str, when: datetime) -> dict:
    """A detection as a proposal with the evidence a person needs to judge it (bounded, printable)."""
    p = analysis.proposal
    root = Path(root)
    parents = Counter(_rel(f.path.parent, root) if f.path.parent != root else "" for f in analysis.projects)
    return clean({
        "id": proposal_id(root, p.pattern, p.depth), "root": root_key(root), "pattern": p.pattern,
        "depth": p.depth, "description": p.description, "depth_text": depth_text(p.depth), "projects": p.projects,
        "detected_at": _iso(when), "examples": [e["path"] for e in p.examples[:MAX_EXAMPLES]],
        "levels": [[level, n] for level, n in p.levels.items()],
        "parents": [[k, v] for k, v in parents.most_common(8)], "parents_total": len(parents),
        "exceptions": p.exceptions[:MAX_LISTED], "exceptions_total": p.exceptions_total,
        "outliers": p.outliers[:8], "duplicates": p.duplicates[:5], "deeper": p.deeper[:5],
        "hints": [{k: h.get(k) for k in ("pattern", "description", "where", "folders", "estimated", "branch",
                                          "examples", "combined", "depth", "beside")} for h in p.hints],
        "guard": p.guard, "notes": p.notes, "inner": p.inner, "unreadable": p.unreadable[:5],
        "bad_names": analysis.bad_names})


def _check_record(analysis: Analysis, root: Path | str, when: datetime) -> dict:
    """The latest detection in short: outcome, reason, the closest candidates, further schemes."""
    p = analysis.proposal
    rec = {"root": root_key(root), "checked_at": _iso(when),
           "outcome": "proposal" if p else "problem" if analysis.problem else "none",
           "reason": analysis.reason or analysis.problem,
           "candidates": [{k: c.get(k) for k in ("pattern", "description", "level", "folders", "where", "examples",
                                                  "verdict", "reason")} for c in analysis.candidates[:6]],
           "levels": analysis.levels[:4], "unreadable": analysis.unreadable[:5], "bad_names": analysis.bad_names}
    if p:
        rec.update(pattern=p.pattern, depth=p.depth, projects=p.projects, id=proposal_id(root, p.pattern, p.depth),
                   hints=[{k: h.get(k) for k in ("pattern", "description", "where", "folders", "estimated", "branch",
                                                  "examples", "combined", "depth", "beside")} for h in p.hints])
    return clean(rec)


def _open_readonly(settings) -> sqlite3.Connection | None:
    path = getattr(settings, "db_path", None)
    if not path or not Path(path).is_file():
        return None
    try:
        return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def _scan_notes(conn: sqlite3.Connection | None, root: Path | str, pattern: str | None) -> dict | None:
    rec = _for_root(conn, NOTES_KEY, root)
    return rec if rec is not None and pattern and rec.get("pattern") == pattern else None


def _next_check(check: dict | None) -> str | None:
    when = _parse_time((check or {}).get("checked_at"))
    return _iso(when + RECHECK_INTERVAL) if when else None


def _waiting_problem(proposal: dict | None, rejected: dict | None, check: dict | None, root: Path | str) -> str:
    """Why nothing is read as a project yet (auto mode without a confirmed scheme), German."""
    if proposal:
        examples = ", ".join((proposal.get("examples") or [])[:2])
        return (f"{PENDING_TEXT}: {proposal['pattern']}, Tiefe {proposal['depth']} ({proposal.get('projects')} "
                f"Projektordner{', z. B. ' + examples if examples else ''}) – auf der Statusseite prüfen und "
                f"übernehmen oder ablehnen, oder: {ACCEPT_HINT}")
    if rejected:
        return (f"Vorschlag zur Projektstruktur abgelehnt ({rejected.get('pattern')}, am "
                f"{local_time_text(rejected.get('rejected_at'))} von {rejected.get('rejected_by')}) – kein "
                "Projektnummern-Schema bestätigt. Bitte PROJECT_NUMBER_PATTERN von Hand einstellen (Vorschlag und "
                "Ordnerübersicht: python -m projektakte.structure) oder die Ordner anpassen; eine geänderte "
                "Erkennung wird wieder vorgeschlagen")
    if check and check.get("outcome") != "proposal":
        return (f"Kein eindeutiges Projektnummern-Schema in PROJECTS_DIR erkannt "
                f"(PROJECT_NUMBER_PATTERN=auto, Prüfung {local_time_text(check.get('checked_at'))}): "
                f"{check.get('reason')} Nächste Prüfung "
                f"{'frühestens ' + local_time_text(nxt) if (nxt := _next_check(check)) else 'beim nächsten Indexierungslauf'}. Bitte "
                "PROJECT_NUMBER_PATTERN einstellen (Vorschlag und Ordnerübersicht: python -m projektakte.structure) "
                f"oder, sobald die Ordner stimmen, das erkannte Schema bestätigen: {ACCEPT_HINT}")
    return ("Projektnummern-Schema noch nicht erkannt (PROJECT_NUMBER_PATTERN=auto) – das geschieht beim "
            "nächsten Indexierungslauf; der Vorschlag gilt erst, wenn er bestätigt ist")


def current_layout(settings, conn: sqlite3.Connection | None = None) -> Layout:
    """The effective layout without detecting anything: the configured pattern, or the confirmed
    scheme of the automatic mode (read from ``conn`` or, without one, the database opened read-only)
    together with a pending proposal, a rejection and the latest detection."""
    raw = getattr(settings, "project_number_pattern", None) or DEFAULT_PROJECT_NUMBER_PATTERN
    configured_depth = getattr(settings, "project_folder_depth", None)
    root = getattr(settings, "projects_dir", None)
    if raw != AUTO:
        problem = pattern_problem(raw)
        return Layout(None if problem else raw, clamp_depth(configured_depth), False, False,
                      problem=f"PROJECT_NUMBER_PATTERN={raw} ist unbrauchbar: {problem}" if problem else None,
                      notes=_scan_notes(conn, root, raw) if conn is not None and root and not problem else None)
    if conn is None and (own := _open_readonly(settings)) is not None:
        with closing(own):
            return current_layout(settings, own)
    confirmed = _for_root(conn, CONFIRMED_KEY, root)
    confirmed = confirmed if _valid_scheme(confirmed) and confirmed.get("confirmed_at") else None
    proposal = _for_root(conn, PROPOSAL_KEY, root)
    proposal = proposal if _valid_scheme(proposal) else None
    rejected = _for_root(conn, REJECTED_KEY, root)
    check = _for_root(conn, CHECK_KEY, root)
    if proposal and ((rejected and rejected.get("id") == proposal.get("id"))
                     or (confirmed and confirmed.get("id") == proposal.get("id"))):
        proposal = None  # written by a detection that ran while the person decided
    # the depth is the confirmed proposal's: the person confirmed evidence computed at that depth, so a
    # configured PROJECT_FOLDER_DEPTH is not used (and said so, see Layout.depth_notice)
    ignored = clamp_depth(configured_depth) if configured_depth is not None else None
    if confirmed:
        return Layout(confirmed["pattern"], confirmed["depth"], True, True, confirmed=confirmed, proposal=proposal,
                      rejected=rejected, check=check, notes=_scan_notes(conn, root, confirmed["pattern"]),
                      ignored_depth=ignored)
    unclear = check is not None and check.get("outcome") != "proposal" and not proposal
    return Layout(None, proposal["depth"] if proposal else DEFAULT_PROJECT_FOLDER_DEPTH, True, True,
                  problem=_waiting_problem(proposal, rejected, check, root), proposal=proposal, rejected=rejected,
                  check=check, next_check=_next_check(check) if unclear else None, ignored_depth=ignored)


def number_pattern(settings, conn: sqlite3.Connection | None = None) -> str:
    """The effective shape of project numbers for finding them in texts (mails, search queries);
    :data:`NO_MATCH` while there is none (not confirmed yet, unusable setting)."""
    return current_layout(settings, conn).pattern or NO_MATCH


def _parse_time(value: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _agrees(confirmed: dict, rec: dict, result: Analysis, root: Path) -> tuple[bool, int | None]:
    """Is a new detection the confirmed scheme? Compared by meaning, not as text: the same set of
    alternatives with the same depth – or exactly the same project folders found. Also returns how
    many folders the confirmed scheme finds now, if that had to be scanned."""
    if alternatives(confirmed["pattern"]) == alternatives(rec["pattern"]) and rec["depth"] == confirmed["depth"]:
        return True, None
    stored = scan_projects(root, confirmed["pattern"], confirmed["depth"], unreadable=[], duplicates=[])
    return bool(stored) and {p.path for p in stored} == {p.path for p in result.projects}, len(stored)


def _apply_detection(conn: sqlite3.Connection, settings, result: Analysis, now: datetime) -> None:
    """Store a detection: always as the latest check; with a clear scheme as a proposal – unless it is
    the confirmed scheme or the rejected proposal. Never touches the confirmed scheme. The caller
    commits."""
    root = Path(settings.projects_dir)
    # read again: a person may have confirmed or rejected while the share was read
    confirmed = _for_root(conn, CONFIRMED_KEY, root)
    confirmed = confirmed if _valid_scheme(confirmed) and confirmed.get("confirmed_at") else None
    pending = _for_root(conn, PROPOSAL_KEY, root)
    rejected = _for_root(conn, REJECTED_KEY, root)
    previous = _for_root(conn, CHECK_KEY, root) or {}
    check = _check_record(result, root, now)
    _save(conn, CHECK_KEY, check)
    _delete(conn, *LEGACY_KEYS)
    if result.proposal is None:
        if check["outcome"] == "none" and pending and result.folders:
            # the share does not show it any more (renamed, reorganised …) – an empty one is not mounted
            _delete(conn, PROPOSAL_KEY)
        if check["reason"] != previous.get("reason"):
            log.warning("Projektnummern-Schema: keine eindeutige Erkennung in %s: %s", printable(str(root)),
                        check["reason"])
        return
    rec = proposal_record(result, root, now)
    if confirmed:
        same, now_found = _agrees(confirmed, rec, result, root)
        if same:
            if pending:
                _delete(conn, PROPOSAL_KEY)
                log.info("Projektnummern-Schema: die neue Erkennung stimmt wieder mit dem bestätigten %s überein",
                         confirmed["pattern"])
            return
        rec["current_projects"] = now_found
    if rejected and rejected.get("id") == rec["id"]:
        if pending:
            _delete(conn, PROPOSAL_KEY)
        return
    if not pending or pending.get("id") != rec["id"]:
        log.warning("Projektnummern-Schema: %s – %s, Tiefe %d (%d Projektordner, z. B. %s)%s. Bestätigen auf der "
                    "Statusseite oder mit: %s", PENDING_TEXT, rec["pattern"], rec["depth"], rec["projects"],
                    ", ".join(rec["examples"][:3]),
                    f"; weiter verwendet wird das bestätigte {confirmed['pattern']}" if confirmed else
                    "; bis dahin werden keine Projekte abgeglichen", ACCEPT_HINT)
    _save(conn, PROPOSAL_KEY, rec)


def _without_folders(root: Path) -> bool:
    """Does ``root`` hold no folder at all (hidden ones aside)? An empty mount point looks like that."""
    try:
        with os.scandir(root) as entries:
            return not any(not e.name.startswith(".") and e.is_dir() for e in entries)
    except OSError:
        return False  # projects_root_problem reports an unreadable folder


def layout_for_index(conn: sqlite3.Connection, settings, *, now: datetime | None = None,
                     max_folders: int = MAX_FOLDERS) -> tuple[Layout, dict]:
    """The layout for an indexing pass. With PROJECT_NUMBER_PATTERN=auto the share is analysed – at
    most every :data:`RECHECK_INTERVAL` (reading a large share costs directory traffic), at once
    after a person asked for it – and the result stored as a proposal; only a confirmed scheme is
    used, never the proposal (stability: a half-mounted share must never change which folders count
    as projects). Returns the layout and a small summary for the run stats."""
    layout = current_layout(settings, conn)
    if not layout.auto:
        return layout, {}
    now = now or _now()
    root = Path(settings.projects_dir)
    info = {"pattern": layout.pattern, "depth": layout.depth, "source": layout.source, "state": layout.state}
    if problem := projects_root_problem(root):
        return (layout if layout.pattern else dataclasses.replace(layout, problem=problem)), info
    if (layout.proposal or layout.confirmed) and _without_folders(root):
        # a share that is suddenly empty is not mounted (like registry.sync_from_folder sees it): nothing
        # is detected, the confirmed scheme and a pending proposal stay as they are
        problem = (f"Projektordner {root} enthält keine Ordner mehr – Laufwerk nicht eingebunden? Die Erkennung "
                   "wartet, bis er wieder Ordner enthält"
                   + ("; der Vorschlag bleibt bestehen" if layout.proposal else ""))
        return (layout if layout.pattern else dataclasses.replace(layout, problem=problem)), info
    checked = _parse_time((layout.check or {}).get("checked_at"))
    if checked is not None and timedelta(0) <= now - checked < RECHECK_INTERVAL:
        return layout, info
    try:
        result = analyse(root, max_folders=max_folders)
    except Exception as exc:  # noqa: BLE001 – a detection problem must never stop the indexing
        log.exception("Projektnummern-Schema: Erkennung fehlgeschlagen")
        result = Analysis(root=str(root), max_depth=MAX_DEPTH,
                          reason=f"Erkennung des Projektnummern-Schemas fehlgeschlagen ({type(exc).__name__}: {exc}).")
    _apply_detection(conn, settings, result, now)
    conn.commit()
    layout = current_layout(settings, conn)
    info = {"pattern": layout.pattern, "depth": layout.depth, "source": layout.source, "state": layout.state,
            "detected": result.proposal is not None}
    if layout.proposal:
        info["proposal"] = layout.proposal["pattern"]
    return layout, info


# --- decisions of a person (status page, command line)
def store_confirmed(conn: sqlite3.Connection, rec: dict, user: str | None, via: str,
                    now: datetime | None = None) -> dict:
    """Confirm a proposal record: it is used from the next indexing pass on, a pending proposal and
    a rejection are forgotten. Logs who confirmed. The caller commits."""
    confirmed = {**rec, "confirmed_at": _iso(now or _now()), "confirmed_by": printable(user or ANONYMOUS),
                 "confirmed_via": via}
    _save(conn, CONFIRMED_KEY, confirmed)
    _delete(conn, PROPOSAL_KEY, REJECTED_KEY, *LEGACY_KEYS)
    who = confirmed["confirmed_by"]
    log.warning("Projektstruktur bestätigt von %s: %s, Tiefe %d (%s Projektordner bei der Erkennung am %s)",
                who if f"({via})" in who else f"{who} ({via})", confirmed["pattern"], confirmed["depth"],
                confirmed.get("projects"), local_time_text(confirmed.get("detected_at")))
    return clean(confirmed)


def _pending(conn: sqlite3.Connection, settings, wanted: str | None) -> dict:
    pending = _for_root(conn, PROPOSAL_KEY, settings.projects_dir)
    if not getattr(settings, "project_pattern_auto", False):
        raise ProposalError("PROJECT_NUMBER_PATTERN ist fest eingestellt – ein Vorschlag gilt nur mit "
                            "PROJECT_NUMBER_PATTERN=auto.")
    if not _valid_scheme(pending):
        raise ProposalError("Es wartet kein Vorschlag zur Projektstruktur auf eine Entscheidung – die Statusseite "
                            "zeigt den aktuellen Stand.")
    if not wanted or wanted != pending.get("id"):
        raise ProposalError("Der Vorschlag hat sich inzwischen geändert (neue Erkennung) – bitte den aktuellen "
                            "Vorschlag auf der Statusseite prüfen und dann entscheiden.")
    return pending


def confirm_proposal(conn: sqlite3.Connection, settings, wanted: str | None, user: str | None,
                     now: datetime | None = None) -> dict:
    """Confirm the pending proposal ``wanted`` (its id, as the page showed it). Raises ProposalError
    if it is not the pending one any more. The caller commits."""
    return store_confirmed(conn, _pending(conn, settings, wanted), user, "Statusseite", now)


def reject_proposal(conn: sqlite3.Connection, settings, wanted: str | None, user: str | None,
                    now: datetime | None = None) -> dict:
    """Reject the pending proposal ``wanted``: the current state stays (a confirmed scheme stays in
    use), and the same proposal is not offered again until the detection changes. The caller commits."""
    pending = _pending(conn, settings, wanted)
    rejected = {k: pending.get(k) for k in ("id", "root", "pattern", "depth", "description", "projects",
                                            "detected_at", "examples")}
    rejected.update(rejected_at=_iso(now or _now()), rejected_by=printable(user or ANONYMOUS))
    _save(conn, REJECTED_KEY, rejected)
    _delete(conn, PROPOSAL_KEY)
    log.warning("Vorschlag zur Projektstruktur abgelehnt von %s: %s, Tiefe %d", rejected["rejected_by"],
                rejected["pattern"], rejected["depth"])
    return clean(rejected)


def request_recheck(conn: sqlite3.Connection, settings, *, forget_rejection: bool = False) -> None:
    """Detect again at the next indexing pass (instead of waiting for :data:`RECHECK_INTERVAL`);
    ``forget_rejection`` offers a rejected proposal again. The caller commits."""
    root = settings.projects_dir
    check = _for_root(conn, CHECK_KEY, root)
    if check is not None:
        _save(conn, CHECK_KEY, {**check, "checked_at": None})
    if forget_rejection:
        _delete(conn, REJECTED_KEY)


def record_scan_notes(conn: sqlite3.Connection, settings, layout: Layout, stats: dict,
                      documents: dict | None = None, now: datetime | None = None) -> None:
    """Keep what the project scan (``stats``) and the documents pass (``documents``) noticed – folders a
    guard excludes, folder and file names in a wrong encoding – for the status page, and log it once:
    when it first appears or changes, not at every pass."""
    if not layout.pattern or not isinstance(stats, dict) or stats.get("warning"):
        return
    root = root_key(settings.projects_dir)
    old = _load(conn, NOTES_KEY) or {}
    if isinstance(documents, dict) and "bad_names" in documents:
        bad_files = ({"count": documents["bad_names"], "examples": documents.get("bad_name_examples") or []}
                     if documents["bad_names"] else None)
    else:  # the documents were not read this time: what the last pass found still holds
        bad_files = old.get("bad_files") if (old.get("root"), old.get("pattern")) == (root, layout.pattern) else None
    notes = {"root": root, "pattern": layout.pattern, "depth": layout.depth,
             "guard": stats.get("guard_excluded"), "bad_names": stats.get("bad_names"), "bad_files": bad_files}
    if all(old.get(k) == clean(v) for k, v in notes.items()):
        return
    if not notes["guard"] and not notes["bad_names"] and not notes["bad_files"]:
        _delete(conn, NOTES_KEY)
        return
    notes["noticed_at"] = _iso(now or _now())
    _save(conn, NOTES_KEY, notes)
    for text in scan_notices(clean(notes)):
        log.warning("Projektordner: %s", text)


# ------------------------------------------------------------------------------------- command line
def current_check(root: Path, settings) -> dict:
    """What the current settings (environment, confirmed scheme) find in ``root``."""
    layout = current_layout(settings)
    depth_setting = getattr(settings, "project_folder_depth", None)
    out = {"setting": settings.project_number_pattern,
           "depth_setting": AUTO if depth_setting is None else depth_setting,
           "depth_ignored": layout.auto and depth_setting is not None,
           "pattern": layout.pattern, "depth": layout.depth, "source": layout.source, "problem": layout.problem,
           "state": layout.state, "pending": (layout.proposal or {}).get("pattern"), "projects": None, "examples": []}
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
    remark = _BRANCH_TEXT.get(h["branch"]) or ("liegt in denselben Ordnern wie die Projektordner"
                                               if h.get("beside") else "")
    line = f"Weiteres Schema{where}: {h['pattern']}, {count} (z. B. {', '.join(h['examples'])})" + (
        f" – {remark}" if remark else "")
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
        if p.inner:
            out.append(f"Nummerierte Unterordner in den Projektordnern ({'etwa ' if p.inner['count'] > 6 else ''}"
                       f"{de_int(p.inner['count'])}, gelten als Teil der Projekte): " + " · ".join(p.inner["examples"]))
        for hint in p.hints:
            out.extend(_hint_lines(hint, p.depth))
        if p.notes:
            out.append("")
            out.append("Bitte prüfen:")
            out.extend(f"  {note}" for note in p.notes)
    if current is not None:
        out.append("")
        depth = current["depth_setting"]
        out.append(f"Aktuelle Einstellung: PROJECT_NUMBER_PATTERN={current['setting']}, PROJECT_FOLDER_DEPTH={depth}")
        if current.get("depth_ignored"):
            out.append("  PROJECT_FOLDER_DEPTH gilt nicht zusammen mit PROJECT_NUMBER_PATTERN=auto – gelesen wird "
                       "die Ordnertiefe des bestätigten Vorschlags (PROJECT_FOLDER_DEPTH weglassen)")
        if current["pattern"] is None:
            out.append(f"  {current['problem']}")
        elif current.get("pending"):
            out.append(f"  {PENDING_TEXT}: {current['pending']}")
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
        out.append("(oder PROJECT_NUMBER_PATTERN=auto: Projektakte schlägt das Schema beim ersten Lauf vor; es gilt "
                   "erst, wenn es auf der Statusseite oder mit --uebernehmen bestätigt ist, und wechselt danach nie "
                   "von selbst)")
    if a.bad_names and p is None:  # with a proposal it is among the points to check
        out.append("")
        out.append(f"Ordnernamen mit falscher Zeichenkodierung: {de_int(a.bad_names['count'])} (z. B. "
                   f"{', '.join(a.bad_names['examples'][:3])}) – {ENCODING_HINT}.")
    return printable("\n".join(out))


def _format_files(f: dict) -> list[str]:
    out = [f"Dateien in {de_count(f['projects'], 'Projektordner', 'Projektordnern')}: "
           f"{de_int(f['files'])} ({de_size(f['bytes'])})"
           + (f" – nach {de_count(f['max_files'], 'Datei', 'Dateien')} abgebrochen" if f["truncated"] else "")]
    read = [e for e in f["extensions"] if e["read"]]
    out.append(f"  liest Projektakte:       {de_int(f['readable'])}"
               + (" – " + ", ".join(f"{e['ext']} {de_int(e['files'])}" for e in read[:8]) if read else ""))
    if f["too_big"]:
        out.append(f"  davon zu groß (über {de_size(MAX_FILE_BYTES)}): {de_int(f['too_big'])}")
    if f.get("bad_names"):
        out.append(f"  nicht lesbar, Name mit falscher Zeichenkodierung: {de_int(f['bad_names'])} – {ENCODING_HINT}")
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
               "Mit PROJECT_NUMBER_PATTERN=auto schlägt Projektakte das erkannte Schema auf der Statusseite vor; "
               "es gilt erst, wenn es dort („Übernehmen“) oder mit --uebernehmen bestätigt ist, und wechselt "
               "danach nie von selbst. --uebernehmen bestätigt das jetzt erkannte Schema (ab dem nächsten "
               "Indexierungslauf).")
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
                      help="das jetzt erkannte Schema für PROJECT_NUMBER_PATTERN=auto bestätigen (ersetzt ein "
                           "früher bestätigtes Schema)")
    return ap


def _cli_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 – no user name in the environment (container): still confirm
        return "unbekannt"


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
            accepted = store_confirmed(conn, proposal_record(analysis, root, _now()), f"{_cli_user()} (Kommandozeile)",
                                       "Kommandozeile")
    if args.json:
        data = {**analysis.to_dict(), "exit_code": analysis.exit_code, "current": current, "files": files,
                "context": context, "env": env_lines(analysis.proposal), "accepted": accepted}
        print(json.dumps(clean(data), ensure_ascii=False, indent=1))
    else:
        print(format_report(analysis, current, files, context))
        if args.uebernehmen:
            print()
            if accepted:
                print(printable(f"Übernommen (bestätigt von {accepted['confirmed_by']}): {accepted['pattern']}, Tiefe "
                                f"{accepted['depth']} – gilt mit PROJECT_NUMBER_PATTERN=auto ab dem nächsten "
                                "Indexierungslauf (Statusseite: „Jetzt neu indexieren“). Projekte, die nicht zum neuen "
                                "Schema passen, werden ausgeblendet und nach der Karenzzeit aus dem Index entfernt."))
                if not settings.project_pattern_auto:
                    print(f"Hinweis: PROJECT_NUMBER_PATTERN ist fest eingestellt "
                          f"({settings.project_number_pattern}) – der gespeicherte Wert gilt erst mit "
                          "PROJECT_NUMBER_PATTERN=auto.")
            else:
                print("Nichts übernommen: kein eindeutiges Schema erkannt.")
    return analysis.exit_code


if __name__ == "__main__":
    sys.exit(main())
