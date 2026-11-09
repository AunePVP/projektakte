"""Project register: projects are folders whose name starts with a project number.

Several folders with the same number are *one* project (:func:`scan_projects`): the order folder and
its part folders that lie outside it (119300-Bilder, 119300-CAD next to the order folder or instead
of it), copies (111301_Heisse Bemessung-2013-11-04) and nested copies of a site. All of them are read;
one of them is the project's primary folder (``projects.path``), the others are its further folders
(table ``project_folders``). How the primary folder and the name are chosen: :func:`folder_kind`,
:func:`project_name`."""
from __future__ import annotations

import itertools
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import DEFAULT_PROJECT_FOLDER_DEPTH, PROJECT_FOLDER_DEPTH_MAX, pattern_problem
# names that are no valid UTF-8 (ENCODING_HINT, has_bad_encoding, printable) are handled in documents.py,
# which reads the files, and imported from here as well
from .documents import (ENCODING_HINT, _sqlite_time, further_folders, has_bad_encoding, hide_project_documents,
                        link_problem, printable, refresh_fts_titles, remove_project_documents,
                        restore_project_documents, warn_once)

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
    path: Path  # the primary folder: real path (for reading); names may hold surrogate escapes, see printable()
    further: tuple[Path, ...] = ()  # further folders with the same number, read as part of the project

    @property
    def folders(self) -> tuple[Path, ...]:
        """All folders of the project, the primary folder first."""
        return (self.path, *self.further)


# ------------------------------------------------------------------------------------- folder kinds
# What follows the number in a project folder's name decides what the folder is (folder_kind):
ORDER = "order"  # "<number>_<name>" or "<number> <name>" (any name after another separator, too): the
#                  order folder itself, carrying the project's name
BARE = "bare"  # only the number: an order folder without a name
PART = "part"  # "<number>-<one word>" (119300-Bilder, 701400-Mail, 747900-ea-200214): a part of the order,
#                the kind of subfolder that belongs inside the order folder – its word names the part,
#                not the project
PART_SEPARATOR = "-"
_KIND_RANK = {ORDER: 0, BARE: 1, PART: 2}
_SITE_HEAD = re.compile(r"(\d+)[\s_\-–—:]*(.*)$", re.DOTALL)


def folder_kind(separator: str, rest: str) -> str:
    """The kind of a project folder from what follows its number: ``separator`` (the characters between
    number and name) and ``rest`` (the name). A part folder is a single hyphen followed by one word
    without spaces – a general rule of form, not a list of words, so it holds for any office's part
    names. (A project named with one word after a hyphen, P20001-Rathaus, is a part folder by form as
    well; on its own it keeps that word as its name, see :func:`project_name`.)"""
    rest = rest.strip()
    if not rest:
        return BARE
    if separator == PART_SEPARATOR and not any(c.isspace() for c in rest):
        return PART
    return ORDER


def site_name(folder: Path, number: str, root: Path) -> str | None:
    """The name of the site (Bauvorhaben) a project folder belongs to: among the folders above it (below
    ``root``) those whose leading number the project number continues with further digits
    (1193_Gasse am Markt above 119300-Bilder) – the longest such number, of equal ones the outermost
    (1015_Altstadt, not the intermediate folder 1015_PDF_100303 inside it) – without its number. None if
    there is none, or it has no name of its own (a year folder 2024, a group folder 11)."""
    best: tuple[int, str] | None = None
    number = number.upper()
    for above in folder.parents:  # nearest first
        if above == root or not above.is_relative_to(root) or above == above.parent:
            break
        m = _SITE_HEAD.match(above.name)
        if not m or not m.group(2).strip():
            continue
        digits = m.group(1)
        if len(number) > len(digits) and number.startswith(digits) and number[len(digits)].isdigit():
            if best is None or len(digits) >= best[0]:  # walking outwards: of equal ones the outermost
                best = (len(digits), m.group(2).strip())
    return best[1] if best else None


def site_fit(folder: Path, number: str, root: Path) -> bool | None:
    """Whether a project folder lies in a site of its own number: True if a folder above it (below
    ``root``) with a leading number and a name is one whose number the project number continues
    (1193_Gasse above 119300_…; a year folder 2013_Archiv in between does not matter); False if there
    are such folders above it but none fits (1827_Werk/Fotos/230512_Begehung – a date, no order of site
    1827); None if there are none (group folders 1/11 have no name, other layouts no sites)."""
    number = number.upper()
    named = False
    for above in folder.parents:
        if above == root or not above.is_relative_to(root) or above == above.parent:
            break
        m = _SITE_HEAD.match(above.name)
        if not m or not m.group(2).strip():
            continue
        digits = m.group(1)
        if len(number) > len(digits) and number.startswith(digits) and number[len(digits)].isdigit():
            return True
        named = True
    return False if named else None


@dataclass(frozen=True)
class _Candidate:
    """A folder whose name starts with a project number, as the scan met it."""
    path: Path
    kind: str
    rest: str  # the name after the number
    depth: int
    seen: int  # position in the scan (folders sorted by name, depth first)

    @property
    def rank(self) -> tuple[int, int, int]:
        """Order of the folders of one project: order folders before bare ones before part folders, then
        the shallowest, then the first in the scan – the first one is the primary folder."""
        return _KIND_RANK[self.kind], self.depth, self.seen


def project_name(number: str, candidates: list[_Candidate], root: Path, *, named_orders: bool = True) -> str:
    """The name of a project (``candidates`` ranked, the primary folder first): the name of its order
    folder. Without one (only part folders 119300-Bilder, 119300-CAD … or bare numbers) the name of the
    site they belong to (:func:`site_name`) – "Gasse am Markt …", not "Bilder" – where they are parts of an
    order filed without it: where most projects of the share are order folders "<number>_<name>" (the
    office's filing, ``named_orders``, :func:`scan_projects`), or where the words of its part folders
    differ (Bilder, CAD: none of them names the project). Otherwise, in shares that name projects
    "<number>-<word>", the word of the first part folder is the name, as it always was (1_Hochbau/1234-Kita
    stays "Kita", not the group folder "Hochbau"); else the number."""
    for c in candidates:
        if c.kind == ORDER:
            return c.rest
    words = {c.rest for c in candidates if c.kind == PART}
    if (named_orders or len(words) > 1) and (site := site_name(candidates[0].path, number, root)):
        return site
    for c in candidates:
        if c.kind == PART:
            return c.rest
    return number


def _distinct(candidates: list[_Candidate], root: Path, boundary: Path) -> list[_Candidate]:
    """The folders of one project without the ones reached twice: a folder that is the same real folder
    as another (a link inside the share) counts once – on its real path, if it is reached that way as
    well – and a folder inside another one of the project (or holding one) is not read a second time.
    ``candidates`` are ranked; the result is ranked as well."""
    kept: list[tuple[_Candidate, Path, bool]] = []
    for c in candidates:
        real = Path(os.path.realpath(c.path))
        try:
            linked = real != boundary / c.path.relative_to(root)
        except ValueError:
            linked = True
        for i, (k, k_real, k_linked) in enumerate(kept):
            if real == k_real:
                if k_linked and not linked:  # the same folder on its real path: that one stays
                    kept[i] = (c, real, linked)
                break
            if real.is_relative_to(k_real) or k_real.is_relative_to(real):
                break
        else:
            kept.append((c, real, linked))
    return sorted((k for k, _, _ in kept), key=lambda k: k.rank)


def _own_site(number: str, candidates: list[_Candidate], root: Path) -> tuple[list[_Candidate], list[_Candidate]]:
    """The folders of one number split by :func:`site_fit`: if one of them lies in a site of the number,
    the ones in a site of another number are strangers (not read with the project), and the folders in
    their own site come first among folders of the same kind. Without any folder in its own site
    (other layouts) nothing changes. Returns (kept, ranked; strangers)."""
    fits = {c.path: site_fit(c.path, number, root) for c in candidates}
    if True not in fits.values():
        return candidates, []
    kept = [c for c in candidates if fits[c.path] is not False]
    kept.sort(key=lambda c: (c.rank[0], fits[c.path] is not True, *c.rank[1:]))
    return kept, [c for c in candidates if fits[c.path] is False]


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


# The guards the detection puts in front of a bare-digit scheme so that years, year ranges and dates
# never count as project numbers (structure.py, rule 5). Only these are guards: a negative lookahead
# written by hand ((?!AN)[A-Z]{2}-\d{4}) is a decision of the admin and excludes nothing to report.
YEAR_GUARD = r"(?!(?:19|20)\d\d(?!\d))"
DATE_GUARD = r"(?!(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d))"
_RANGE_HEAD, _RANGE_TAIL = r"(?!(?:19|20)\d\d", r"(?:19|20)\d\d(?!\d))"
# the separator between the two years as structure._sep_regex writes it: -, _, \., \s or a class of
# them, optionally followed by ?
_RANGE_GUARD = re.compile(re.escape(_RANGE_HEAD) + r"(?:-|_|\\\.|\\s|\[(?:-|_|\.|\\s)+\])?\??"
                          + re.escape(_RANGE_TAIL))


def year_range_guard(separator: str) -> str:
    """The guard against a year range (2017-2018) for a scheme of two four-digit groups."""
    return _RANGE_HEAD + separator + _RANGE_TAIL


def is_guard(text: str) -> bool:
    """Is ``text`` (a negative lookahead) one of the guards the detection generates?"""
    return text in (YEAR_GUARD, DATE_GUARD) or _RANGE_GUARD.fullmatch(text) is not None


def loose_pattern(pattern: str) -> str | None:
    """The expression without its guards – the year, year-range and date guards the detection puts at
    the start of an alternative (:func:`is_guard`), e.g. (?!(?:19|20)\\d\\d(?!\\d))\\d{4} → \\d{4} – or
    None if it has none. Matches of the loose expression that the real one refuses are the folders a
    guard excludes. Other negative lookaheads (written by hand) stay: what they exclude is intended."""
    parts, changed = [], False
    for alt in _split_alternatives(_unwrap(pattern)):
        alt = alt.strip()
        while alt.startswith("(?!") and (end := _group_end(alt, 0)) > 0 and is_guard(alt[:end]):
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
    (separators after the number dropped, see :func:`head_kind`). Raises ``re.error`` for an unusable
    pattern."""
    return re.compile(rf"^(?P<num>{pattern})(?![0-9A-Za-z])[\s_\-–—:]*(?P<name>.*)$", re.IGNORECASE)


def head_kind(m: re.Match) -> str:
    """The :func:`folder_kind` of a folder name matched by :func:`project_head` (the separators are what
    lies between number and name – no group of its own, so no name a pattern might use is taken)."""
    return folder_kind(m.string[m.end("num"):m.start("name")], m.group("name"))


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
                  bad_names: list[Path] | None = None,
                  foreign: list[tuple[str, Path, Path]] | None = None) -> list[ProjectFolder]:
    """Find project folders below ``root`` (e.g. ``2024/BS-2024-0137 Grundschule …``).

    A folder whose name starts with a project number is a project folder; its subfolders are not
    searched for further projects. Other folders are searched down to ``max_depth`` levels below
    ``root`` (PROJECT_FOLDER_DEPTH: 1 = only directly in ``root``). Hidden folders and links pointing
    outside ``root`` are skipped. Folders that cannot be listed are appended to ``unreadable``.

    All folders with the same number are one project: the primary folder is the first order folder
    ("<number>_<name>", "<number> <name>"), else a folder with the bare number, else a part folder
    ("<number>-<one word>") – of these the shallowest, then the first by name (:class:`_Candidate`);
    the others are its further folders, read with it. The name: :func:`project_name`. The same real
    folder reached twice (a link inside the share) counts once. If one folder of a number lies in a
    site of that number and another one in a site of another number (:func:`site_fit` – a date folder
    1827_Werk/Fotos/230512_Begehung next to the order 2305_Bau/230512_Konzept), the one in the foreign
    site is not read with the project: ``foreign`` receives (number, folder, primary folder) – without
    that list a warning names it. ``duplicates`` receives (number, primary folder, further folder) for
    every further folder – a sign of a wrong scheme for the structure detection, a hint for the filing
    check (numbers may be wrong). ``excluded`` receives
    (number, folder) of the folders a guard of the pattern keeps out (a year 1999 for
    (?!(?:19|20)\\d\\d(?!\\d))\\d{4}) although they lie at the level of project folders and hold none – no
    guard may drop projects silently. ``bad_names`` receives the folders whose name is no valid UTF-8.
    Nothing is ever written.
    """
    head = project_head(pattern)
    loose_text = loose_pattern(pattern) if excluded is not None else None
    loose = project_head(loose_text) if loose_text else None
    max_depth = clamp_depth(max_depth)
    candidates: dict[str, list[_Candidate]] = {}
    found_depths: set[int] = set()
    guarded: list[tuple[str, Path, int]] = []
    order = itertools.count()
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
                candidates.setdefault(m.group("num").upper(), []).append(
                    _Candidate(child, head_kind(m), m.group("name").strip(), depth, next(order)))
                found_depths.add(depth)
                continue
            if loose is not None and (lm := loose.match(child.name)):
                guarded.append((lm.group("num").upper(), child, depth))
            if depth < max_depth:
                walk(child, depth + 1)

    walk(root, 1)
    ranked: dict[str, list[_Candidate]] = {}
    for number, cands in candidates.items():
        cands = sorted(cands, key=lambda c: c.rank)
        if len(cands) > 1:
            cands = _distinct(cands, root, boundary)
        if len(cands) > 1:
            cands, strangers = _own_site(number, cands, root)
            for c in strangers:
                if foreign is not None:
                    foreign.append((number, c.path, cands[0].path))
                    continue  # the caller reports it
                warn_once(log, "Ordner %s trägt die Nummer %s, liegt aber in einem Bauvorhaben mit anderer Nummer "
                               "– wird nicht mit dem Projekt %s gelesen (Nummer prüfen, z. B. ein Datum)",
                          printable(str(c.path)), number, printable(str(cands[0].path)))
        ranked[number] = cands
    # how the share names its projects: mostly "<number>_<name>" (then a folder without a name is part of
    # an order, named after its site) or otherwise ("1234-Kita", bare numbers: the word is the name)
    named_orders = sum(c[0].kind == ORDER for c in ranked.values()) * 2 >= len(ranked)
    projects: list[ProjectFolder] = []
    for number, cands in ranked.items():
        primary = cands[0]
        projects.append(ProjectFolder(number, project_name(number, cands, root, named_orders=named_orders),
                                      primary.path, tuple(c.path for c in cands[1:])))
        if duplicates is not None:
            duplicates.extend((number, primary.path, c.path) for c in cands[1:])
    if guarded and excluded is not None:
        holding = {a for p in projects for f in p.folders for a in f.parents}
        excluded.extend((number, folder) for number, folder, depth in guarded
                        if depth in found_depths and folder not in holding)
    return sorted(projects, key=lambda p: p.number)


def sync_projects(conn: sqlite3.Connection, folders: list[ProjectFolder], *,
                  unreadable: list[Path] | tuple = ()) -> dict:
    """Store scanned projects and keep the derived data in step.

    A project whose folders all disappeared is marked inactive, not deleted (its mails and decisions
    stay). Its files are hidden from search and answers at once and deleted only after the grace
    period (:data:`MISSING_GRACE` and :data:`MISSING_RUNS`); a folder that comes back before brings
    them back as they were. Projects with a folder below a folder that could not be listed
    (``unreadable``) are left alone. The further folders of a project (several folders with its number)
    are stored in ``project_folders``; a further folder the scan no longer finds is dropped – the
    documents pass then removes its files like vanished files – unless it lies below a folder that
    could not be listed: then it stays. A new name refreshes the full-text titles of all sections.
    Names that are no valid UTF-8 are stored with � (:func:`printable`) – the database only holds
    valid text. ``folders_changed`` counts the projects whose further folders changed."""
    existing = {r["number"]: r for r in conn.execute("SELECT * FROM projects")}
    known_further = further_folders(conn)
    now = _utcnow()
    stats = {"projects": len(folders), "added": 0, "updated": 0, "reactivated": 0, "deactivated": 0,
             "documents_hidden": 0, "documents_restored": 0, "documents_removed": 0, "folders_changed": 0}
    blocked = [Path(printable(str(p))) for p in unreadable]

    def below_blocked(path: str) -> bool:
        return any(Path(path).is_relative_to(b) for b in blocked)

    seen: set[str] = set()
    for f in folders:
        number, name, path = printable(f.number), printable(f.name), printable(str(f.path))
        if number in seen:  # two numbers that differ only in bytes that are no UTF-8
            continue
        seen.add(number)
        further = list(dict.fromkeys(p for p in (printable(str(x)) for x in f.further) if p != path))
        row = existing.get(number)
        old_further = known_further.get(number, [])
        if row is not None:  # known folders the scan could not see (a folder above them unreadable) stay
            listed = {path, *further}
            further += [old for old in dict.fromkeys([row["path"], *old_further])
                        if old not in listed and below_blocked(old)]
        if row is None:
            conn.execute("INSERT INTO projects(number, name, path) VALUES(?, ?, ?)", (number, name, path))
            stats["added"] += 1
        elif (row["name"], row["path"], row["active"]) != (name, path, 1):
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
        if further != old_further or (row is not None and row["path"] != path and old_further):
            _store_further(conn, number, further)
            stats["folders_changed"] += 1
            before = [row["path"], *old_further] if row is not None else []
            gone = [old for old in before if old not in further and old != path]
            if gone:
                log.info("Projekt %s: %d bisherige(r) Ordner nicht mehr dabei (z. B. %s – umbenannt, verschoben, "
                         "gelöscht oder fremde Nummer); die Dateien darin werden verschoben oder aus dem Index "
                         "entfernt", number, len(gone), gone[0])
    cutoff = _sqlite_time(now - MISSING_GRACE)
    for number, row in existing.items():
        if number in seen or any(below_blocked(p) for p in (row["path"], *known_further.get(number, ()))):
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


def _store_further(conn: sqlite3.Connection, number: str, further: list[str]) -> None:
    conn.execute("DELETE FROM project_folders WHERE project = ?", (number,))
    conn.executemany("INSERT INTO project_folders(project, path, position) VALUES(?, ?, ?)",
                     [(number, path, i) for i, path in enumerate(further, start=1)])


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
    if merged := [f for f in folders if f.further]:
        # one line per pass at most, and only when the folders of a project changed – several folders
        # with one number are normal in many shares (part folders, copies), no warning
        stats["merged"] = {"projects": len(merged), "folders": sum(len(f.further) for f in merged),
                           "examples": [f"{printable(f.number)}: {len(f.folders)} Ordner" for f in merged[:MAX_NOTED]]}
        (log.info if stats["folders_changed"] else log.debug)(
            "Projektordner: %d Projektnummer(n) mit mehreren Ordnern – werden je als ein Projekt gelesen "
            "(%d weitere Ordner, z. B. %s)", len(merged), stats["merged"]["folders"],
            ", ".join(stats["merged"]["examples"][:3]))
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


def project_folders(conn: sqlite3.Connection, number: str) -> list[str]:
    """All folders of a project as stored, the primary folder first (empty for an unknown project)."""
    row = conn.execute("SELECT number, path FROM projects WHERE number = ? COLLATE NOCASE", (number,)).fetchone()
    if row is None:
        return []
    return [row["path"], *further_folders(conn, row["number"]).get(row["number"], [])]


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
