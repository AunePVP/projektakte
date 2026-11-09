#!/usr/bin/env python3
"""Ablage-Prüfung: prüft, ob die Projektablage nach der Büro-Regel aufgebaut ist, und was Projektakte
davon liest.

Regel (anpassbar im Block REGELN unten):

    <Ziffer>/<2 Ziffern>/<4-stellig>_<Bauvorhaben>/<6-stellig>_<Auftrag>/<6-stellig><Trenner><Unterordner>
    z. B. 2/28/2842_Beratung …/284201_Zwischendecke …/284201-Mail, 284201_Abgabe_FW

Ordner ohne Zahl vorne werden nicht geprüft (und ihre Dateien nicht vermisst) – außer im
Auftragsordner: Dort muss jeder Unterordner mit der Auftragsnummer beginnen.

Liest nur Ordner- und Dateinamen, nie Inhalte, und verändert nichts. Was Projektakte liest, ermittelt
das Skript mit dem Scan der App selbst: Mehrere Ordner mit derselben Auftragsnummer sind ein Auftrag
und werden zusammen gelesen (auch Unterordner wie 119300-Bilder, die ohne ihren Auftragsordner im
Bauvorhaben liegen). Meldet zwei Arten von Abweichungen:

    FEHLER   betrifft Projektakte oder die Regel im Auftrag: ein Auftrag wird nicht gefunden, ein
             Ordner mit fremder 6-stelliger Zahl (z. B. ein Datum) wird als Auftrag gelesen, ein
             Unterordner im Auftrag hat keine Nummer, Dateien in nummerierten Ordnern liegen außerhalb
             eines Auftrags …
    HINWEIS  ein nummerierter Ordner weicht von der Form ab (Trennzeichen, Ebene, Nummer passt nicht …),
             mehrere Ordner haben dieselbe Auftragsnummer (werden zusammen gelesen – stimmt die
             Nummer?), ein Unterordner liegt ohne seinen Auftragsordner im Bauvorhaben

Aufruf (im Repository, mit der Python-Umgebung von Projektakte):

    .venv/bin/python scripts/ablage_pruefen.py /mnt/projekt
    .venv/bin/python scripts/ablage_pruefen.py /mnt/projekt --csv abweichungen.csv

Exit-Code: 0 = keine Fehler (Hinweise möglich), 1 = Fehler gefunden, 2 = Ordner nicht lesbar,
3 = falscher Aufruf.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere in the repository

from projektakte.documents import MAX_FILE_BYTES, has_bad_encoding, printable  # noqa: E402
from projektakte.extract import SUPPORTED_EXT  # noqa: E402
from projektakte.registry import PART, head_kind, project_head, scan_projects, site_fit  # noqa: E402

# ------------------------------------------------------------------------------------------- REGELN
# The office's filing rule – adjust here if the numbering changes.
PATTERN = r"\d{6}"  # PROJECT_NUMBER_PATTERN: the orders (Aufträge) are the projects
DEPTH = 4  # PROJECT_FOLDER_DEPTH: Ziffer / 2 Ziffern / Bauvorhaben / Auftrag
ORDER_DIGITS, SITE_DIGITS = 6, 4
# typical subfolder names of an order (checked only with --kategorien). Which folders are part folders
# outside their order folder (119300-Bilder in the site) is decided by Projektakte's own rule, not by
# this list: "<number>-<one word>" (registry.folder_kind)
CATEGORIES = ("Ausgang", "Bilder", "CAD", "LV", "Mail", "Sonstiges", "Tab", "Text")
LEVEL1 = re.compile(r"^(\d)$")  # 2
LEVEL2 = re.compile(r"^(\d{2})$")  # 28
SITE = re.compile(r"^(\d{4})_(\S.*)$")  # 2842_Beratung Lidl …  (Bauvorhaben)
ORDER = re.compile(r"^(\d{6})_(\S.*)$")  # 284201_Zwischendecke …  (Auftrag)
CATEGORY = re.compile(r"^(\d{6})[-_ ](\S.*)$")  # 284201-Mail, 284201_Abgabe_FW
NUMBER_START = re.compile(r"^(\d+)(.?)")  # a name that starts with a number, and what follows it
NOISE = {"thumbs.db", "desktop.ini", ".ds_store"}  # files Windows/macOS create by themselves

# kind → (severity, title); the order here is the order of the report
KINDS = {
    "nicht_gefunden": ("FEHLER", "Auftrag wird von Projektakte nicht gefunden"),
    "fremde_nummer": ("FEHLER", "Ordner mit 6-stelliger Zahl passt nicht zum Bauvorhaben (z. B. ein Datum) – "
                      "Nummer prüfen"),
    "unterordner_ohne_nummer": ("FEHLER", "Ordner im Auftrag ohne Nummer vorne"),
    "dateien_ausserhalb": ("FEHLER", "Dateien in nummerierten Ordnern außerhalb eines Auftrags – werden nicht gelesen"),
    "nicht_lesbar": ("FEHLER", "Ordner nicht lesbar (Rechte?) – Inhalt fehlt in Projektakte"),
    "doppelt": ("HINWEIS", "Mehrere Ordner mit derselben Auftragsnummer – werden zusammen gelesen (Nummer prüfen)"),
    "kategorie_als_projekt": ("HINWEIS", "Unterordner ohne seinen Auftragsordner – wird mit seinem Auftrag gelesen"),
    "ebene1": ("HINWEIS", "Ebene 1: nummerierter Ordner ist nicht eine Ziffer"),
    "ebene2": ("HINWEIS", "Ebene 2: nummerierter Ordner ist nicht zwei Ziffern (passend zur Ebene 1)"),
    "bauvorhaben_name": ("HINWEIS", "Ebene 3: nummerierter Ordner nicht in der Form 1234_Name"),
    "bauvorhaben_nummer": ("HINWEIS", "Bauvorhaben-Nummer passt nicht zum Sammelordner"),
    "auftrag_ohne_bauvorhaben": ("HINWEIS", "Auftrag liegt nicht in einem Bauvorhaben-Ordner"),
    "auftrag_name": ("HINWEIS", "Nummerierter Ordner im Bauvorhaben nicht in der Form 123456_Name"),
    "bauvorhaben_leer": ("HINWEIS", "Bauvorhaben ohne Auftrag"),
    "unterordner_nummer": ("HINWEIS", "Unterordner im Auftrag mit anderer Nummer als der Auftrag"),
    "kategorie_unbekannt": ("HINWEIS", "Unbekannte Kategorie (nur mit --kategorien)"),
    "kategorie_fehlt": ("HINWEIS", "Auftrag ohne alle Standard-Kategorien (nur mit --kategorien)"),
    "dateien_im_auftrag": ("HINWEIS", "Dateien direkt im Auftragsordner (werden gelesen, liegen aber in keinem Unterordner)"),
    "auftrag_leer": ("HINWEIS", "Auftrag ohne Dateien"),
    "kodierung": ("HINWEIS", "Name mit falscher Zeichenkodierung (Freigabe mit iocharset=utf8 einbinden)"),
    "verknuepfung": ("HINWEIS", "Verknüpfung (Symlink) – wird von diesem Skript nicht verfolgt"),
}


class Report:
    def __init__(self, root: Path):
        self.root = root
        self.issues: dict[str, list[tuple[str, str]]] = defaultdict(list)
        self.counts = Counter()
        self.ext_in = Counter()  # extension → files inside projects
        self.too_big = 0
        self.outside: Counter = Counter()  # folder (relative) → files outside any project
        self.missing_categories = Counter()
        self.standard: list[str] = []  # categories at least half of the orders have

    def rel(self, path: Path) -> str:
        try:
            text = str(path.relative_to(self.root))
        except ValueError:
            text = str(path)
        return printable(text) or "."

    def add(self, kind: str, path: Path, detail: str = "") -> None:
        self.issues[kind].append((self.rel(path), detail))


def _entries(folder: Path, report: Report) -> list[os.DirEntry]:
    try:
        with os.scandir(folder) as it:
            return sorted(it, key=lambda e: e.name)
    except OSError as exc:
        report.add("nicht_lesbar", folder, exc.strerror or str(exc))
        return []


def check(root: Path, *, pattern: str = PATTERN, depth: int = DEPTH, categories=None,
          sizes: bool = False, progress=None) -> Report:
    """Walk the share once (names only) and compare it with the filing rule and with what Projektakte's
    own scan finds. ``categories``: also check the names of the subfolders in an order against this list
    (unknown ones, and the ones most orders have but this one lacks); None: only their number."""
    report = Report(root)
    head = project_head(pattern)
    foreign: list[tuple[str, Path, Path]] = []
    # the app's scan: folders with one number are one project; a folder with the number of an order in a
    # site of another number is not read with it (foreign)
    found = scan_projects(root, pattern, depth, foreign=foreign)
    projects = {str(folder): p for p in found for folder in p.folders}  # every folder read → its project
    strangers = {str(folder) for _, folder, _ in foreign}  # read by no project
    report.counts["projekte"] = len(found)
    report.counts["mehrere_ordner"] = sum(1 for p in found if p.further)
    by_number = {p.number for p in found}
    misplaced: set[str] = set()  # folders read as (part of) an order that are no order folder themselves
    parts: set[str] = set()  # part folders outside their order folder (reported as such, not by their form)
    part_labels: dict[str, set[str]] = defaultdict(set)  # order → names of its part folders outside it
    for p in found:
        for folder in p.folders:
            m = head.match(folder.name)
            kind = head_kind(m)
            together = f", gelesen mit {report.rel(p.path)}" if folder != p.path else ""
            if site_fit(folder, p.number, root) is False:
                # a six-digit number that is no order of its site – often a date (231020_Bilder …, also deeper:
                # 1827_X/Fotos/230512_Begehung); a year folder in between (1827_X/2013_Archiv/182701_…) is fine
                report.add("fremde_nummer", folder, f"Bauvorhaben {_site_number(folder, root) or '?'}, gelesen als "
                           f"Auftrag {p.number}" + together)
                misplaced.add(str(folder))
            elif kind == PART:
                report.add("kategorie_als_projekt", folder, f"Auftrag {p.number} „{p.name}“" + together)
                misplaced.add(str(folder))
                parts.add(str(folder))
                part_labels[p.number].add(m.group("name").strip())
            elif folder != p.path:
                report.add("doppelt", folder, f"{p.number}, zusammen gelesen mit {report.rel(p.path)}")
    for number, folder, primary in foreign:  # the number of an order of another site: not read at all
        report.add("fremde_nummer", folder, f"Bauvorhaben {_site_number(folder, root) or '?'} – nicht gelesen, "
                   f"Auftrag {number} ist {report.rel(primary)}")
    wanted = set(categories or ())
    orders: dict[str, tuple[Path, set[str]]] = {}  # order → its first order folder, the categories it has
    sites_with_orders: set[str] = set()  # sites holding a folder read as an order or in the order form

    def walk(folder: Path, level: int, chain: dict, in_project: bool) -> int:
        """Check ``folder`` and everything below it; returns the number of files below it."""
        entries = _entries(folder, report)
        report.counts["ordner"] += 1
        if progress and report.counts["ordner"] % 1000 == 0:
            progress(report.counts["ordner"], report.counts["dateien"])
        is_order = str(folder) in projects and str(folder) not in misplaced
        files_here = total = 0
        cats_here: set[str] = set()
        for e in entries:
            name = e.name
            path = Path(e.path)
            if name.startswith(".") or name.startswith("~$") or name.lower() in NOISE:
                continue
            if has_bad_encoding(name) and not chain.get("ignored"):
                report.add("kodierung", path)
            if e.is_symlink():
                if not chain.get("ignored"):
                    report.add("verknuepfung", path)
                continue
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if not is_dir:
                report.counts["dateien"] += 1
                files_here += 1
                if in_project:
                    ext = Path(name).suffix.lower()
                    report.ext_in[ext] += 1
                    if sizes and ext in SUPPORTED_EXT:
                        try:
                            if e.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES:
                                report.too_big += 1
                        except OSError:
                            pass
                elif level >= 1 and not chain.get("ignored"):  # in numbered folders only (not the share root)
                    # grouped by the order folder they lie in (lost or skipped), else at most one level
                    # below the order level
                    key = chain.get("group") or "/".join(Path(report.rel(folder)).parts[:depth + 1]) or "."
                    report.outside[key] += 1
                continue
            child_level = level + 1
            sub_chain = dict(chain)
            project = projects.get(str(path))
            if project is not None:
                sub_chain["order"] = project.number
            inside = in_project or project is not None
            numbered = name[:1].isdigit()
            if (project is not None or ORDER.match(name)) and chain.get("site_path"):
                sites_with_orders.add(chain["site_path"])  # also deeper: an intermediate folder, a nested copy
            if ORDER.match(name):
                if not inside and not chain.get("group"):
                    sub_chain["group"] = report.rel(path)  # an order folder Projektakte does not read
            if is_order:  # a folder directly inside an order: must start with the order's number
                start = NUMBER_START.match(name)
                if not start:
                    report.add("unterordner_ohne_nummer", path, f"Auftrag {chain.get('order', '')}".strip())
                elif chain.get("order") and start.group(1) != chain["order"]:
                    report.add("unterordner_nummer", path, f"Auftrag {chain['order']}")
                elif wanted:
                    m = CATEGORY.match(name)
                    label = m.group(2) if m else ""
                    if label in wanted:
                        cats_here.add(label)
                    elif label:
                        report.add("kategorie_unbekannt", path, label)
            elif not in_project and not chain.get("ignored"):
                if not numbered:
                    sub_chain["ignored"] = True  # no number in front: not part of the numbered filing
                elif str(path) not in parts:  # (a part folder is reported as such, not by its form)
                    _rule(report, path, name, child_level, chain, sub_chain,
                          project is not None or str(path) in strangers, depth)
            if str(path) in strangers:  # reported as "fremde_nummer"; what lies inside belongs to it
                sub_chain["lost"] = True
                sub_chain["group"] = report.rel(path)
            elif (not inside and not chain.get("lost") and not sub_chain.get("ignored") and ORDER.match(name)
                    and (lost := head.match(name))):
                # only the outermost: folders such as 104900_Archiv inside a lost order belong to it
                elsewhere = "; die Nummer wird an anderer Stelle gelesen" if lost.group("num").upper() in by_number else ""
                report.add("nicht_gefunden", path, f"liegt auf Ebene {child_level}, gelesen wird bis Ebene {depth}"
                           + elsewhere)
                sub_chain["lost"] = True
            total += walk(path, child_level, sub_chain, inside)
        total += files_here
        if is_order:  # the categories of an order read from several folders count together
            orders.setdefault(projects[str(folder)].number, (folder, set()))[1].update(cats_here)
            if files_here:
                report.add("dateien_im_auftrag", folder, f"{files_here} Datei(en)")
            if not total:
                report.add("auftrag_leer", folder)
        if chain.get("site_path") == str(folder) and str(folder) not in sites_with_orders:
            report.add("bauvorhaben_leer", folder)
        return total

    walk(root, 0, {}, False)
    if wanted:  # "standard" = what most orders have (a category of a newer template is no standard yet)
        for number, (_, cats) in orders.items():  # part folders outside the order folder count as well
            cats.update(part_labels.get(number, set()) & wanted)
        present = Counter(c for _, cats in orders.values() for c in cats)
        report.standard = [c for c in categories if orders and present[c] * 2 >= len(orders)]
        for folder, cats in orders.values():
            missing = set(report.standard) - cats
            if missing:
                report.missing_categories.update(missing)
                report.add("kategorie_fehlt", folder, ", ".join(sorted(missing)))
    for folder, n in report.outside.items():
        report.add("dateien_ausserhalb", report.root / folder if folder != "." else report.root, f"{n} Datei(en)")
    return report


def _site_number(folder: Path, root: Path) -> str | None:
    """The number of the nearest folder above ``folder`` (below ``root``) in the form of a site
    (1234_Name), e.g. 1827 for 1/18/1827_X/Fotos/230512_Begehung – or None."""
    for above in folder.parents:
        if above == root or not above.is_relative_to(root):
            return None
        if SITE.match(above.name):
            return above.name[:SITE_DIGITS]
    return None


def _separator(name: str, digits: int) -> str:
    start = NUMBER_START.match(name)
    if start and len(start.group(1)) == digits and start.group(2) != "_":
        return f"Trennzeichen nach der Nummer: „{start.group(2)}“ statt „_“"
    return ""


def _rule(report: Report, path: Path, name: str, level: int, chain: dict, sub: dict, found: bool,
          depth: int = DEPTH) -> None:
    """The filing rule for a numbered folder above the order level; records what it is in ``sub``.
    ``found``: Projektakte reads it as a project (an order); ``depth``: PROJECT_FOLDER_DEPTH."""
    digits = NUMBER_START.match(name).group(1)
    if level == 1:
        m = LEVEL1.match(name)
        if m:
            sub["l1"] = m.group(1)
        else:
            report.add("ebene1", path)
    elif level == 2:
        m = LEVEL2.match(name)
        if m and (not chain.get("l1") or m.group(1).startswith(chain["l1"])):
            sub["l2"] = m.group(1)
        else:
            report.add("ebene2", path, "4-stellig: Bauvorhaben ohne 2-stelligen Ordner" if len(digits) == SITE_DIGITS
                       else "")
    elif level == 3:
        m = SITE.match(name)
        if m:
            sub["site"], sub["site_path"] = m.group(1), str(path)
            if chain.get("l2") and not m.group(1).startswith(chain["l2"]):
                report.add("bauvorhaben_nummer", path, f"Sammelordner {chain['l2']}")
        elif len(digits) == ORDER_DIGITS:
            report.add("auftrag_ohne_bauvorhaben", path)
        else:
            if len(digits) == SITE_DIGITS:  # a site with another separator is still the site
                sub["site"], sub["site_path"] = digits, str(path)
            report.add("bauvorhaben_name", path, _separator(name, SITE_DIGITS))
    elif level == 4 and chain.get("site"):
        if len(digits) == ORDER_DIGITS:
            if not digits.startswith(chain["site"]):
                if not found:  # read as an order or left out as foreign: reported as "fremde_nummer" already
                    report.add("auftrag_name", path, f"Nummer passt nicht zum Bauvorhaben {chain['site']}")
            elif not ORDER.match(name):
                report.add("auftrag_name", path, _separator(name, ORDER_DIGITS))
        else:
            lost = " – darin liegende Aufträge werden nicht gefunden" if level >= depth else ""
            report.add("auftrag_name", path, f"keine 6-stellige Auftragsnummer{lost}" if len(digits) == SITE_DIGITS
                       else "")


# ------------------------------------------------------------------------------------------- output
def _de(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def format_report(report: Report, *, pattern: str, depth: int, examples: int, seconds: float) -> str:
    out = [f"Ablage-Prüfung: {printable(str(report.root))}  (nur Namen gelesen, nichts verändert)",
           f"Gelesen: {_de(report.counts['ordner'])} Ordner, {_de(report.counts['dateien'])} Dateien in "
           f"{seconds:.0f} s", "",
           f"Was Projektakte liest (PROJECT_NUMBER_PATTERN={pattern}, PROJECT_FOLDER_DEPTH={depth})",
           f"  Projekte (Aufträge) gefunden:       {_de(report.counts['projekte'])}"]
    if report.counts["mehrere_ordner"]:
        out.append(f"  davon aus mehreren Ordnern:         {_de(report.counts['mehrere_ordner'])} (werden je als ein "
                   "Auftrag zusammen gelesen)")
    in_projects = sum(report.ext_in.values())
    readable = sum(n for ext, n in report.ext_in.items() if ext in SUPPORTED_EXT)
    outside = sum(report.outside.values())
    out.append(f"  Dateien in Aufträgen:               {_de(in_projects)}, davon liest Projektakte {_de(readable)}"
               + (f" (davon zu groß, über 80 MB: {_de(report.too_big)})" if report.too_big else ""))
    unread = [(ext or "(ohne Endung)", n) for ext, n in report.ext_in.most_common() if ext not in SUPPORTED_EXT]
    if unread:
        out.append("  nicht gelesene Dateitypen:          "
                   + ", ".join(f"{ext} {_de(n)}" for ext, n in unread[:10])
                   + (f" … ({len(unread) - 10} weitere)" if len(unread) > 10 else ""))
    out.append(f"  Dateien außerhalb von Aufträgen:    {_de(outside)}" + (" – werden nicht gelesen" if outside else ""))
    out.append("")
    errors = sum(len(report.issues[k]) for k, (sev, _) in KINDS.items() if sev == "FEHLER")
    hints = sum(len(report.issues[k]) for k, (sev, _) in KINDS.items() if sev == "HINWEIS")
    if not errors and not hints:
        out.append("Keine Abweichungen – die Ablage folgt der Regel.")
        return "\n".join(out)
    out.append(f"Abweichungen: {_de(errors)} Fehler (betreffen Projektakte), {_de(hints)} {'Hinweis' if hints == 1 else 'Hinweise'} (nur Ablage-Regel)")
    for kind, (severity, title) in KINDS.items():
        items = report.issues.get(kind)
        if not items:
            continue
        out.append("")
        out.append(f"{severity:7} {title}: {_de(len(items))}")
        if kind == "kategorie_fehlt":
            out.append("        Standard: " + ", ".join(report.standard))
            out.append("        am häufigsten fehlt: " + ", ".join(
                f"{c} ({_de(n)})" for c, n in report.missing_categories.most_common()))
        for path, detail in items[:examples]:
            out.append(f"        {path}" + (f"  – {detail}" if detail else ""))
        if len(items) > examples:
            out.append(f"        … und {_de(len(items) - examples)} weitere (alle mit --csv)")
    return "\n".join(out)


def write_csv(report: Report, target: Path) -> None:
    with open(target, "w", newline="", encoding="utf-8-sig") as f:  # BOM: Excel opens umlauts correctly
        w = csv.writer(f, delimiter=";")
        w.writerow(["Schwere", "Art", "Pfad", "Detail"])
        for kind, (severity, title) in KINDS.items():
            for path, detail in report.issues.get(kind, ()):
                w.writerow([severity, title, path, detail])


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # German, and exit code 3 (2 means: folder not readable)
        self.print_usage(sys.stderr)
        print(f"{self.prog}: Fehler: {message}", file=sys.stderr)
        raise SystemExit(3)


def main(argv: list[str] | None = None) -> int:
    ap = _Parser(prog="ablage_pruefen.py", description="Prüft die Projektablage gegen die Büro-Regel "
                 "(nur Namen, nichts wird verändert).", add_help=False)
    ap.add_argument("pfad", nargs="?", default=os.environ.get("PROJECTS_DIR"), help="Projektordner (Standard: "
                    "PROJECTS_DIR)")
    ap.add_argument("--csv", type=Path, metavar="DATEI", help="alle Abweichungen als CSV (für Excel) schreiben")
    ap.add_argument("--beispiele", type=int, default=10, metavar="N", help="Beispiele je Abweichung (Standard 10)")
    ap.add_argument("--muster", default=PATTERN, help=f"PROJECT_NUMBER_PATTERN (Standard {PATTERN})")
    ap.add_argument("--tiefe", type=int, default=DEPTH, help=f"PROJECT_FOLDER_DEPTH (Standard {DEPTH})")
    ap.add_argument("--kategorien", nargs="?", const=",".join(CATEGORIES), default=None, metavar="LISTE",
                    help="auch die Namen der Unterordner im Auftrag prüfen (Standard-Liste: "
                    f"{','.join(CATEGORIES)}; eigene Liste mit Komma getrennt)")
    ap.add_argument("--groessen", action="store_true", help="Dateigrößen prüfen (über 80 MB liest Projektakte "
                    "nicht; langsamer über das Netz)")
    ap.add_argument("-h", "--hilfe", "--help", action="help", help="diese Hilfe anzeigen")
    args = ap.parse_args(argv)
    if not args.pfad:
        ap.error("Projektordner angeben (oder PROJECTS_DIR setzen)")
    root = Path(args.pfad)
    if not root.is_dir():
        print(f"FEHLER: {root} ist kein lesbarer Ordner (Freigabe eingebunden?)", file=sys.stderr)
        return 2
    try:
        project_head(args.muster)
    except re.error as exc:
        ap.error(f"--muster ist kein gültiger regulärer Ausdruck ({exc})")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    def progress(folders: int, files: int) -> None:
        print(f"\r… {_de(folders)} Ordner, {_de(files)} Dateien", end="", file=sys.stderr, flush=True)

    started = time.monotonic()
    app_log = logging.getLogger("projektakte")
    level = app_log.level
    app_log.setLevel(logging.ERROR)  # what the app's scan warns about is in the report
    try:
        report = check(root, pattern=args.muster, depth=args.tiefe,
                       categories=(tuple(c.strip() for c in args.kategorien.split(",") if c.strip())
                                   if args.kategorien else None),
                       sizes=args.groessen, progress=progress if sys.stderr.isatty() else None)
    finally:
        app_log.setLevel(level)
    if sys.stderr.isatty():
        print("\r" + " " * 60 + "\r", end="", file=sys.stderr)
    print(format_report(report, pattern=args.muster, depth=args.tiefe, examples=max(0, args.beispiele),
                        seconds=time.monotonic() - started))
    if args.csv:
        write_csv(report, args.csv)
        print(f"\nAlle Abweichungen: {args.csv}")
    return 1 if any(report.issues.get(k) for k, (sev, _) in KINDS.items() if sev == "FEHLER") else 0


if __name__ == "__main__":
    raise SystemExit(main())
