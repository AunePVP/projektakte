#!/usr/bin/env python3
"""Ablage-Prüfung: prüft, ob die Projektablage nach der Büro-Regel aufgebaut ist, und was Projektakte
davon liest.

Regel (anpassbar im Block REGELN unten):

    <Ziffer>/<2 Ziffern>/<4-stellig>_<Bauvorhaben>/<6-stellig>_<Auftrag>/<6-stellig>-<Kategorie>
    z. B. 2/28/2842_Beratung …/284201_Zwischendecke …/284201-Mail

Liest nur Ordner- und Dateinamen, nie Inhalte, und verändert nichts. Meldet zwei Arten von
Abweichungen:

    FEHLER   betrifft Projektakte: ein Auftrag wird nicht gefunden, eine Nummer ist doppelt, Dateien
             liegen außerhalb eines Auftrags und werden nicht gelesen …
    HINWEIS  weicht nur von der Ablage-Regel ab (Trennzeichen, Nummer passt nicht zum Bauvorhaben,
             unbekannte Kategorie, fehlende Standard-Ordner …)

Aufruf (im Repository, mit der Python-Umgebung von Projektakte):

    .venv/bin/python scripts/ablage_pruefen.py /mnt/projekt
    .venv/bin/python scripts/ablage_pruefen.py /mnt/projekt --csv abweichungen.csv

Exit-Code: 0 = keine Fehler (Hinweise möglich), 1 = Fehler gefunden, 2 = Ordner nicht lesbar,
3 = falscher Aufruf.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere in the repository

from projektakte.documents import MAX_FILE_BYTES, has_bad_encoding, printable  # noqa: E402
from projektakte.extract import SUPPORTED_EXT  # noqa: E402
from projektakte.registry import project_head, scan_projects  # noqa: E402

# ------------------------------------------------------------------------------------------- REGELN
# The office's filing rule – adjust here if the numbering changes.
PATTERN = r"\d{6}"  # PROJECT_NUMBER_PATTERN: the orders (Aufträge) are the projects
DEPTH = 4  # PROJECT_FOLDER_DEPTH: Ziffer / 2 Ziffern / Bauvorhaben / Auftrag
CATEGORIES = ("Ausgang", "Bilder", "CAD", "LV", "Mail", "Sonstiges", "Tab", "Text")
LEVEL1 = re.compile(r"^(\d)$")  # 2
LEVEL2 = re.compile(r"^(\d{2})$")  # 28
SITE = re.compile(r"^(\d{4})_(\S.*)$")  # 2842_Beratung Lidl …  (Bauvorhaben)
ORDER = re.compile(r"^(\d{6})_(\S.*)$")  # 284201_Zwischendecke …  (Auftrag)
CATEGORY = re.compile(r"^(\d{6})-(\S.*)$")  # 284201-Mail
NUMBER_START = re.compile(r"^(\d+)(.?)")  # a name that starts with a number, and what follows it
NOISE = {"thumbs.db", "desktop.ini", ".ds_store"}  # files Windows/macOS create by themselves

# kind → (severity, title); the order here is the order of the report
KINDS = {
    "nicht_gefunden": ("FEHLER", "Auftrag wird von Projektakte nicht gefunden"),
    "doppelt": ("FEHLER", "Auftragsnummer doppelt – nur der erste Ordner wird gelesen"),
    "kategorie_als_projekt": ("FEHLER", "Kategorie-Ordner ohne Auftragsordner – wird als eigenes Projekt gelesen"),
    "dateien_ausserhalb": ("FEHLER", "Dateien außerhalb eines Auftrags – werden nicht gelesen"),
    "nicht_lesbar": ("FEHLER", "Ordner nicht lesbar (Rechte?) – Inhalt fehlt in Projektakte"),
    "ebene1": ("HINWEIS", "Ebene 1: Name ist nicht eine Ziffer"),
    "ebene2": ("HINWEIS", "Ebene 2: Name ist nicht zwei Ziffern (passend zur Ebene 1)"),
    "bauvorhaben_name": ("HINWEIS", "Ebene 3: Bauvorhaben-Ordner nicht in der Form 1234_Name"),
    "bauvorhaben_nummer": ("HINWEIS", "Bauvorhaben-Nummer passt nicht zum Sammelordner"),
    "auftrag_ohne_bauvorhaben": ("HINWEIS", "Auftrag liegt nicht in einem Bauvorhaben-Ordner"),
    "auftrag_name": ("HINWEIS", "Ordner im Bauvorhaben nicht in der Form 123456_Name"),
    "auftrag_nummer": ("HINWEIS", "Auftragsnummer beginnt nicht mit der Bauvorhaben-Nummer"),
    "bauvorhaben_leer": ("HINWEIS", "Bauvorhaben ohne Auftrag"),
    "kategorie_name": ("HINWEIS", "Ordner im Auftrag nicht in der Form 123456-Kategorie"),
    "kategorie_nummer": ("HINWEIS", "Kategorie-Ordner mit anderer Nummer als der Auftrag"),
    "kategorie_unbekannt": ("HINWEIS", "Unbekannte Kategorie"),
    "kategorie_fehlt": ("HINWEIS", "Auftrag ohne alle Standard-Kategorien (die mindestens die Hälfte der Aufträge hat)"),
    "dateien_im_auftrag": ("HINWEIS", "Dateien direkt im Auftragsordner (werden gelesen, liegen aber in keiner Kategorie)"),
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


def check(root: Path, *, pattern: str = PATTERN, depth: int = DEPTH, categories=CATEGORIES,
          sizes: bool = False, progress=None) -> Report:
    """Walk the share once (names only) and compare it with the filing rule and with what Projektakte's
    own scan finds."""
    report = Report(root)
    head = project_head(pattern)
    duplicates: list[tuple[str, Path, Path]] = []
    found = scan_projects(root, pattern, depth, duplicates=duplicates)
    projects = {str(p.path): p for p in found}
    report.counts["projekte"] = len(found)
    for number, first, skipped in duplicates:
        report.add("doppelt", skipped, f"{number}, gelesen wird {report.rel(first)}")
    misplaced: set[str] = set()  # category folders read as projects – reported once, not as orders as well
    for p in found:
        name = p.path.name
        m = CATEGORY.match(name)
        if m and m.group(2) in categories:
            report.add("kategorie_als_projekt", p.path, f"Projekt {p.number} „{m.group(2)}“")
            misplaced.add(str(p.path))
    skipped_paths = {str(s) for _, _, s in duplicates}
    wanted = set(categories)

    def walk(folder: Path, level: int, chain: dict, in_project: bool) -> int:
        """Check ``folder`` and everything below it; returns the number of files below it."""
        entries = _entries(folder, report)
        report.counts["ordner"] += 1
        if progress and report.counts["ordner"] % 1000 == 0:
            progress(report.counts["ordner"], report.counts["dateien"])
        is_order = str(folder) in projects and str(folder) not in misplaced
        files_here = total = 0
        cats_here: set[str] = set()
        order_child = False
        for e in entries:
            name = e.name
            path = Path(e.path)
            if name.startswith(".") or name.startswith("~$") or name.lower() in NOISE:
                continue
            if has_bad_encoding(name):
                report.add("kodierung", path)
            if e.is_symlink():
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
                else:  # grouped by the folder at most one level below the project level (the order)
                    parts = Path(report.rel(folder)).parts
                    report.outside["/".join(parts[:depth + 1]) or "."] += 1
                continue
            child_level = level + 1
            sub_chain = dict(chain)
            project = projects.get(str(path))
            if project is not None:
                sub_chain["order"] = project.number
            inside = in_project or project is not None
            if ORDER.match(name):
                order_child = True
            # the filing rule, level by level – down to the order folder; below a category anything goes
            if not in_project:
                _rule(report, path, name, child_level, chain, sub_chain)
            elif is_order:  # a folder directly inside an order: should be "<order>-<Kategorie>"
                m = CATEGORY.match(name)
                if not m:
                    report.add("kategorie_name", path)
                else:
                    if chain.get("order") and m.group(1) != chain["order"]:
                        report.add("kategorie_nummer", path, f"Auftrag {chain['order']}")
                    if m.group(2) not in wanted:
                        report.add("kategorie_unbekannt", path, m.group(2))
                    else:
                        cats_here.add(m.group(2))
            if (not inside and not chain.get("lost") and ORDER.match(name) and head.match(name)
                    and str(path) not in skipped_paths):
                # only the outermost: folders such as 104900_Archiv inside a lost order belong to it
                report.add("nicht_gefunden", path, f"liegt auf Ebene {child_level}, gelesen wird bis Ebene {depth}")
                sub_chain["lost"] = True
            total += walk(path, child_level, sub_chain, inside)
        total += files_here
        if is_order:
            orders.append((folder, cats_here))
            if files_here:
                report.add("dateien_im_auftrag", folder, f"{files_here} Datei(en)")
            if not total:
                report.add("auftrag_leer", folder)
        if chain.get("site_path") == str(folder) and not order_child:
            report.add("bauvorhaben_leer", folder)
        return total

    orders: list[tuple[Path, set[str]]] = []
    walk(root, 0, {}, False)
    # "standard" categories are those most orders have (a category of a newer template is no standard yet)
    present = Counter(c for _, cats in orders for c in cats)
    report.standard = [c for c in categories if orders and present[c] * 2 >= len(orders)]
    for folder, cats in orders:
        missing = set(report.standard) - cats
        if missing:
            report.missing_categories.update(missing)
            report.add("kategorie_fehlt", folder, ", ".join(sorted(missing)))
    for folder, n in report.outside.items():
        report.add("dateien_ausserhalb", report.root / folder if folder != "." else report.root, f"{n} Datei(en)")
    return report


def _rule(report: Report, path: Path, name: str, level: int, chain: dict, sub: dict) -> None:
    """The filing rule for a folder above the order level; records what the folder is in ``sub``."""
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
            report.add("ebene2", path)
    elif level == 3:
        m = SITE.match(name)
        if m:
            sub["site"], sub["site_path"] = m.group(1), str(path)
            if chain.get("l2") and not m.group(1).startswith(chain["l2"]):
                report.add("bauvorhaben_nummer", path, f"Sammelordner {chain['l2']}")
        elif ORDER.match(name):
            report.add("auftrag_ohne_bauvorhaben", path)
            sub["order"] = ORDER.match(name).group(1)
        else:
            start = NUMBER_START.match(name)
            detail = ""
            if start and len(start.group(1)) == 4 and start.group(2) != "_":
                detail = f"Trennzeichen nach der Nummer: „{start.group(2)}“ statt „_“"
            report.add("bauvorhaben_name", path, detail)
    elif level == 4:
        m = ORDER.match(name)
        if m:
            sub["order"] = m.group(1)
            if chain.get("site") and not m.group(1).startswith(chain["site"]):
                report.add("auftrag_nummer", path, f"Bauvorhaben {chain['site']}")
        elif chain.get("site"):
            start = NUMBER_START.match(name)
            detail = ""
            if start and len(start.group(1)) == 6 and start.group(2) != "_":
                detail = f"Trennzeichen nach der Nummer: „{start.group(2)}“ statt „_“"
            report.add("auftrag_name", path, detail)
    elif ORDER.match(name):
        sub["order"] = ORDER.match(name).group(1)


# ------------------------------------------------------------------------------------------- output
def _de(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def format_report(report: Report, *, pattern: str, depth: int, examples: int, seconds: float) -> str:
    out = [f"Ablage-Prüfung: {printable(str(report.root))}  (nur Namen gelesen, nichts verändert)",
           f"Gelesen: {_de(report.counts['ordner'])} Ordner, {_de(report.counts['dateien'])} Dateien in "
           f"{seconds:.0f} s", "",
           f"Was Projektakte liest (PROJECT_NUMBER_PATTERN={pattern}, PROJECT_FOLDER_DEPTH={depth})",
           f"  Projekte (Aufträge) gefunden:       {_de(report.counts['projekte'])}"]
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
    out.append(f"Abweichungen: {_de(errors)} Fehler (betreffen Projektakte), {_de(hints)} Hinweise (nur Ablage-Regel)")
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
    ap.add_argument("--kategorien", default=",".join(CATEGORIES), help="erwartete Kategorien, mit Komma getrennt")
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
    report = check(root, pattern=args.muster, depth=args.tiefe,
                   categories=tuple(c.strip() for c in args.kategorien.split(",") if c.strip()),
                   sizes=args.groessen, progress=progress if sys.stderr.isatty() else None)
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
