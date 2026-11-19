"""Export page images for an OCR model comparison (``scripts/ocr_vergleich.py``, run on another computer).

    python -m projektakte.ocr_testseiten --anzahl 50 --ziel /data/ocr-test
    python -m projektakte.ocr_testseiten --datei "DG-Ausbau_Teil 1" --seiten 49,56 --ziel /data/ocr-test
    python -m projektakte.ocr_testseiten --erneuern --ziel /data/training

Takes scan pages waiting for the Bildauswertung (a mix over many documents, oldest file dates first so that old
scans are in) or the given pages of one file, prepares each page image exactly as the model gets it now
(extract.page_images, VISION_DPI) – in an extraction worker, the files are untrusted – and writes it with the text
Tesseract or the text layer gave for it (``…​.tesseract.txt``) and a ``seiten.json``. Nothing in the database is
changed. The pages are real project data: they stay on the office network.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
from pathlib import Path

from . import db, extract_worker
from .config import load_settings
from .documents import _in_folder, printable
from .extract import IMAGE_EXT
from .vision import WAITING, VisionPass

log = logging.getLogger("projektakte.ocr_testseiten")

# folders whose pages never go to the model in operation (CAD plans, photos): not taken for the comparison either
DEFAULT_SKIP = ("CAD", "Bilder")
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/tiff": ".tif", "image/webp": ".webp"}


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:60].strip("_") or "seite"


def _candidates(conn, count: int, seed: int, skip: tuple[str, ...] = DEFAULT_SKIP) -> list[tuple[int, int]]:
    """(document id, page) of pending scan pages: at most two pages per document, spread over many documents, none
    from folders named in ``skip`` (CAD, Bilder: "CAD" also matches 304000-CAD and the folders below it)."""
    rows = conn.execute(
        f"SELECT p.doc_id, p.page, d.rel_path FROM page_texts p JOIN documents d ON d.id = p.doc_id "
        f"WHERE p.state IN {WAITING} AND d.ext = '.pdf'").fetchall()
    by_doc: dict[int, list[int]] = {}
    for doc_id, page, rel_path in rows:
        if skip and _in_folder(rel_path, "", skip):
            continue
        by_doc.setdefault(doc_id, []).append(page)
    rnd = random.Random(seed)
    docs = list(by_doc)
    rnd.shuffle(docs)
    picked: list[tuple[int, int]] = []
    for doc_id in docs:
        pages = by_doc[doc_id]
        rnd.shuffle(pages)
        for page in pages[:2]:
            picked.append((doc_id, page))
            if len(picked) >= count:
                return picked
    return picked


# share of each kind of page in a training export (see _training_candidates)
TRAINING_MIX = (("scan", 0.55), ("cad", 0.15), ("digital", 0.15), ("foto", 0.15))


def _training_candidates(conn, count: int, seed: int, exclude: set[tuple[str, int]] = frozenset(),
                         *, mails: bool = True) -> list[tuple[int, int, str]]:
    """(document id, page, kind) for a training set: every kind of page the model should learn – scan pages waiting
    for the model (any folder), plans from CAD folders (also files not read, SKIP_READ_FOLDERS), digital PDFs
    (data sheets, plans with a text layer) and image files (photos) – at most two pages per document, without the
    pages in ``exclude`` ((path inside the project, page), e.g. the comparison set: it must stay unseen).
    ``mails=False``: no mail attachments, only files of the project folders."""
    only_files = "" if mails else " AND d.mail_id IS NULL"
    rnd = random.Random(seed)
    pools: dict[str, list[tuple[int, int]]] = {kind: [] for kind, _share in TRAINING_MIX}
    pending: dict[int, list[int]] = {}
    for doc_id, page, rel_path in conn.execute(
            f"SELECT p.doc_id, p.page, d.rel_path FROM page_texts p JOIN documents d ON d.id = p.doc_id "
            f"WHERE p.state IN {WAITING} AND d.ext = '.pdf'{only_files}"):
        if not _in_folder(rel_path, "", ("CAD",)):  # plans of CAD folders are the kind "cad"
            pending.setdefault(doc_id, []).append(page)
    for doc_id, pages in pending.items():
        rnd.shuffle(pages)
        pools["scan"] += [(doc_id, p) for p in pages[:2]]
    for doc_id, rel_path, ext, pages in conn.execute(
            f"SELECT id, rel_path, ext, pages FROM documents d WHERE 1 = 1{only_files}"):
        if ext == ".pdf" and _in_folder(rel_path, "", ("CAD",)):
            pools["cad"].append((doc_id, rnd.randint(1, pages) if pages else 1))
        elif ext == ".pdf" and doc_id not in pending and pages:
            pools["digital"].append((doc_id, rnd.randint(1, pages)))
        elif ext in IMAGE_EXT:
            pools["foto"].append((doc_id, 1))
    paths = dict(conn.execute("SELECT id, rel_path FROM documents"))
    picked: list[tuple[int, int, str]] = []
    for kind, share in TRAINING_MIX:
        pool = [c for c in pools[kind] if (printable(paths.get(c[0], "")), c[1]) not in exclude]
        rnd.shuffle(pool)
        picked += [(d, p, kind) for d, p in pool[:max(1, round(count * share))]]
    rnd.shuffle(picked)
    return picked[:count]


def _all_candidates(conn, count: int, seed: int, exclude: set[tuple[str, int]] = frozenset(), *,
                    mails: bool = True, per_doc: int = 3) -> list[tuple[int, int, str]]:
    """(document id, page, kind) drawn evenly over every PDF and image file in the index – all folders, read or not,
    whatever state the pages are in – up to ``per_doc`` random pages per document. For a page-type classifier,
    which needs many pages of every kind rather than the mix of the transcription training."""
    rnd = random.Random(seed)
    image_ext = tuple(IMAGE_EXT)
    rows = conn.execute(
        "SELECT id, rel_path, ext, COALESCE(pages, (SELECT MAX(page) FROM page_texts WHERE doc_id = d.id), "
        "(SELECT MAX(page) FROM chunks WHERE doc_id = d.id)) FROM documents d WHERE (ext = '.pdf' OR ext IN (%s))%s"
        % (",".join("?" * len(image_ext)), "" if mails else " AND d.mail_id IS NULL"), image_ext).fetchall()
    rows = [tuple(r) for r in rows]
    rnd.shuffle(rows)
    picked: list[tuple[int, int, str]] = []
    for doc_id, rel_path, ext, pages in rows:
        if ext in IMAGE_EXT:
            choice, kind = [1], "foto"
        else:
            numbers = list(range(1, (pages or 1) + 1))
            rnd.shuffle(numbers)
            choice = numbers[:max(1, per_doc)]
            kind = "cad" if _in_folder(rel_path, "", ("CAD",)) else "pdf"
        for page in choice:
            if (printable(rel_path), page) in exclude:
                continue
            picked.append((doc_id, page, kind))
            if len(picked) >= count:
                return picked
    return picked


def _excluded(files: list[str]) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for f in files:
        for e in json.loads(Path(f).read_text(encoding="utf-8")):
            out.add((e["dokument"], int(e["seite"])))
    return out


def _named(conn, name: str, pages: list[int]) -> list[tuple[int, int]]:
    row = conn.execute("SELECT id, pages FROM documents WHERE rel_path LIKE ? AND ext = '.pdf' ORDER BY id LIMIT 1",
                       (f"%{name}%",)).fetchone()
    if row is None:
        raise SystemExit(f"Keine PDF-Datei mit „{name}“ im Namen gefunden")
    return [(row[0], p) for p in (pages or range(1, (row[1] or 1) + 1))]


def _known_text(conn, doc_id: int, page: int) -> str:
    row = conn.execute("SELECT text FROM page_texts WHERE doc_id = ? AND page = ?", (doc_id, page)).fetchone()
    if row is not None:
        return row[0] or ""
    return "\n".join(t for (t,) in conn.execute(
        "SELECT text FROM chunks WHERE doc_id = ? AND page = ? ORDER BY ord", (doc_id, page)))


_OLD = re.compile(r"^\d{3}_.*\.(jpg|png|tif|webp|bin|tesseract\.txt)$")


def _clear(target: Path) -> int:
    """Remove the pages of an earlier export (``NNN_….jpg`` and their texts, ``seiten.json``): with them left, a new
    export with the same numbers mixed old and new pages, and ``seiten.json`` described only the new ones."""
    removed = 0
    for f in target.iterdir():
        if f.is_file() and (_OLD.match(f.name) or f.name == "seiten.json"):
            f.unlink()
            removed += 1
    return removed


def fit(mime: str, data: bytes, max_size: tuple[int, int] | None) -> tuple[str, bytes]:
    """Scale a page image down to fit ``max_size``, keeping its aspect ratio – never distorted, never enlarged.
    The box turns with the image: the longer side may be up to the larger value (1664x2048 allows a landscape
    page up to 2048x1664). Images that fit are returned unchanged; JPEG stays JPEG, everything else becomes PNG."""
    if not max_size:
        return mime, data
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        short, long_ = sorted(max_size)
        box = (long_, short) if w > h else (short, long_)
        scale = min(box[0] / w, box[1] / h)
        if scale >= 1:
            return mime, data
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        small = img.convert("RGB" if img.mode not in ("RGB", "L") else img.mode).resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    if mime == "image/jpeg":
        small.save(buf, "JPEG", quality=90)
        return mime, buf.getvalue()
    small.save(buf, "PNG", optimize=True)
    return "image/png", buf.getvalue()


def _render(conn, vp, worker, doc_id: int, pages: list[int], max_size) -> tuple[dict | None, list]:
    """(document row, [(page, mime, image, known text)]) of one document; nothing written."""
    row = conn.execute("SELECT id, project, path, rel_path, filename, ext, sha256, cache_path, mail_id "
                       "FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return None, []
    data = vp._read(dict(row))
    if data is None:
        log.warning("%s nicht lesbar – übersprungen", printable(row["rel_path"]))
        return None, []
    if row["ext"] in IMAGE_EXT:  # an image file goes to the model as it is
        from .extract import PageImages

        prepared = [PageImages(1, [(IMAGE_EXT[row["ext"]], data)])]
    else:
        prepared = extract_worker.page_images(row["filename"], data, sorted(pages), vp.prepare, worker=worker)
    done = []
    for p in prepared:
        if not p.images:
            log.info("%s Seite %d: kein Bild (%s)", printable(row["rel_path"]), p.number, p.problem)
            continue
        mime, image = fit(*p.images[0], max_size)
        done.append((p.number, mime, image, _known_text(conn, doc_id, p.number)))
    return dict(row), done


def export(conn, settings, target: Path, wanted: list[tuple], *, worker=None,
           max_size: tuple[int, int] | None = None, parallel: int = 1) -> list[dict]:
    """Write the pages; ``parallel`` documents at a time, each in its own reading process and database
    connection (numbers and names stay in the order of ``wanted``)."""
    target.mkdir(parents=True, exist_ok=True)
    if _clear(target):
        log.info("Seiten eines früheren Exports in %s entfernt", target)
    by_doc: dict[int, list[int]] = {}
    kinds: dict[tuple[int, int], str] = {}
    for doc_id, page, *kind in wanted:
        by_doc.setdefault(doc_id, []).append(page)
        if kind:
            kinds[(doc_id, page)] = kind[0]
    out: list[dict] = []

    def write(row: dict | None, done: list) -> None:
        for number, mime, image, known in done:
            stem = f"{len(out) + 1:03d}_{_safe(Path(row['filename']).stem)}_S{number}"
            (target / f"{stem}{_EXT.get(mime, '.bin')}").write_bytes(image)
            (target / f"{stem}.tesseract.txt").write_text(known, encoding="utf-8")
            entry = {"datei": f"{stem}{_EXT.get(mime, '.bin')}", "dokument": printable(row["rel_path"]),
                     "seite": number, "mime": mime, "tesseract_zeichen": len(known)}
            if (row["id"], number) in kinds:
                entry["quelle"] = kinds[(row["id"], number)]
            out.append(entry)
        if len(out) and len(out) % 200 < len(done):
            log.info("%d Seiten …", len(out))

    docs = list(by_doc.items())
    db_path = getattr(settings, "db_path", None)
    if parallel <= 1 or db_path is None or len(docs) < 2:
        vp = VisionPass(conn, None, projects_dir=getattr(settings, "projects_dir", None), worker=worker)
        for doc_id, pages in docs:
            write(*_render(conn, vp, worker, doc_id, pages, max_size))
    else:
        import threading
        from concurrent.futures import ThreadPoolExecutor

        local = threading.local()
        opened: list = []
        lock = threading.Lock()

        def job(item):
            if not hasattr(local, "vp"):  # one connection and one reading process per thread
                local.conn = db.connect(db_path)
                local.worker = extract_worker.own_worker(settings)
                local.vp = VisionPass(local.conn, None, projects_dir=getattr(settings, "projects_dir", None),
                                      worker=local.worker)
                with lock:
                    opened.append((local.conn, local.worker))
            return _render(local.conn, local.vp, local.worker, item[0], item[1], max_size)

        try:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                window = parallel * 4  # bounded: rendered pages wait in memory only for a few documents
                for i in range(0, len(docs), window):
                    for result in pool.map(job, docs[i : i + window]):
                        write(*result)
        finally:
            for c, w in opened:
                if w is not None:
                    w.close()
                c.close()
    (target / "seiten.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def renew(conn, settings, target: Path, *, worker=None, max_size: tuple[int, int] | None = None) -> tuple[int, int]:
    """Write the page images of the PDF pages listed in ``target``/seiten.json again, as the model gets them now,
    under the same numbers and names (an export whose pages were already corrected or transcribed keeps them):
    only the image file is replaced (with another extension if its type changed) and its entry updated. Texts
    (``….tesseract.txt``), image files and other files are left as they are. Returns (written, not written)."""
    listing = target / "seiten.json"
    entries = json.loads(listing.read_text(encoding="utf-8"))
    vp = VisionPass(conn, None, projects_dir=getattr(settings, "projects_dir", None), worker=worker)
    rows = {printable(r["rel_path"]): r for r in conn.execute(
        "SELECT id, project, path, rel_path, filename, ext, sha256, cache_path, mail_id FROM documents "
        "WHERE ext = '.pdf' ORDER BY id DESC")}  # the oldest row of a path wins
    by_doc: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.get("dokument", "").lower().endswith(".pdf"):
            by_doc.setdefault(entry["dokument"], []).append(entry)
    written, missed = 0, 0
    for path, todo in by_doc.items():
        row = rows.get(path)
        data = vp._read(dict(row)) if row is not None else None
        if data is None:
            log.warning("%s nicht gefunden oder nicht lesbar – %d Seite(n) bleiben", path, len(todo))
            missed += len(todo)
            continue
        pages = {p.number: p for p in extract_worker.page_images(
            row["filename"], data, sorted({int(e["seite"]) for e in todo}), vp.prepare, worker=worker)}
        for entry in todo:
            page = pages.get(int(entry["seite"]))
            if page is None or not page.images:
                log.warning("%s Seite %s: kein Bild (%s) – %s bleibt", path, entry["seite"],
                            page.problem if page else "?", entry["datei"])
                missed += 1
                continue
            mime, image = fit(*page.images[0], max_size)
            old = target / entry["datei"]
            new = old.with_suffix(_EXT.get(mime, ".bin"))
            new.write_bytes(image)
            if new != old and old.exists():
                old.unlink()
            entry["datei"], entry["mime"] = new.name, mime
            written += 1
    listing.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    return written, missed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m projektakte.ocr_testseiten",
                                 description="Seitenbilder für einen Vergleich von OCR-Modellen exportieren")
    ap.add_argument("--ziel", default="/data/ocr-test", help="Zielordner (Voreinstellung: /data/ocr-test)")
    ap.add_argument("--anzahl", type=int, default=50, help="so viele wartende Scan-Seiten (Voreinstellung: 50)")
    ap.add_argument("--datei", help="stattdessen Seiten dieser Datei (Teil des Namens)")
    ap.add_argument("--seiten", default="", help="mit --datei: Seitennummern, z. B. 49,56 (sonst alle)")
    ap.add_argument("--seed", type=int, default=1, help="Zufallsauswahl wiederholbar (Voreinstellung: 1)")
    ap.add_argument("--training", action="store_true",
                    help="Trainingsseiten: gemischt aus wartenden Scans, CAD-Plänen, digitalen PDF-Dateien und Fotos "
                         "(alle Ordner, auch nicht gelesene)")
    ap.add_argument("--ohne-seiten", action="append", default=[],
                    help="mit --training: Seiten aus dieser seiten.json auslassen (der Prüfsatz), mehrfach")
    ap.add_argument("--ohne-ordner", default=",".join(DEFAULT_SKIP),
                    help="keine Seiten aus Ordnern dieses Namens, kommagetrennt (Voreinstellung: CAD,Bilder; "
                         "dazu SKIP_READ_FOLDERS und VISION_SKIP_FOLDERS); leer: alle")
    ap.add_argument("--alle", action="store_true",
                    help="Seiten gleichmäßig aus allen PDF- und Bilddateien aller Ordner, gelesen oder nicht, ohne "
                         "feste Mischung (z. B. für einen Seitentyp-Klassifizierer)")
    ap.add_argument("--je-dokument", type=int, default=3, help="mit --alle: höchstens so viele Seiten je Dokument")
    ap.add_argument("--ohne-mails", action="store_true",
                    help="mit --training oder --alle: keine Mail-Anhänge, nur Dateien aus den Projektordnern")
    ap.add_argument("--max-aufloesung", metavar="BREITExHOEHE",
                    help="Seitenbilder höchstens so groß, z. B. 1664x2048 – verkleinert mit gleichem "
                         "Seitenverhältnis (nie verzerrt, nie vergrößert)")
    ap.add_argument("--parallel", type=int, default=min(8, os.cpu_count() or 1),
                    help="so viele Dokumente gleichzeitig, je in einem eigenen Leseprozess (Voreinstellung: Kerne, "
                         "höchstens 8)")
    ap.add_argument("--erneuern", action="store_true",
                    help="die PDF-Seitenbilder eines früheren Exports in --ziel neu schreiben, wie das Modell sie jetzt "
                         "bekommt – unter denselben Nummern; Texte und übrige Dateien bleiben")
    args = ap.parse_args(argv)
    max_size = None
    if args.max_aufloesung:
        try:
            w, h = (int(x) for x in args.max_aufloesung.lower().replace("×", "x").split("x"))
            assert w > 0 and h > 0
            max_size = (w, h)
        except (ValueError, AssertionError):
            ap.error("--max-aufloesung: BREITExHOEHE erwartet, z. B. 1664x2048")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    conn = db.connect(settings.db_path)
    if args.training or args.erneuern or args.alle:  # CAD plans are part of the training: drawn, not passed over (VISION_SKIP_CAD)
        os.environ["VISION_SKIP_CAD"] = "0"
    worker = extract_worker.own_worker(settings)
    try:
        if args.erneuern:
            if not (Path(args.ziel) / "seiten.json").is_file():
                print(f"Keine seiten.json in {args.ziel}.")
                return 1
            written, missed = renew(conn, settings, Path(args.ziel), worker=worker, max_size=max_size)
            print(f"{written} PDF-Seitenbild(er) in {args.ziel} neu geschrieben"
                  + (f", {missed} nicht (siehe Meldungen)." if missed else "."))
            return 0 if not missed else 1
        if args.alle:
            wanted = _all_candidates(conn, max(1, args.anzahl), args.seed, _excluded(args.ohne_seiten),
                                     mails=not args.ohne_mails, per_doc=args.je_dokument)
        elif args.training:
            wanted = _training_candidates(conn, max(1, args.anzahl), args.seed, _excluded(args.ohne_seiten),
                                          mails=not args.ohne_mails)
        elif args.datei:
            pages = [int(x) for x in args.seiten.split(",") if x.strip()]
            wanted = _named(conn, args.datei, pages)
        else:
            skip = tuple(dict.fromkeys(
                [w.strip() for w in args.ohne_ordner.split(",") if w.strip()]
                + list(getattr(settings, "skip_read_folders", ())) + list(getattr(settings, "vision_skip_folders", ()))))
            wanted = _candidates(conn, max(1, args.anzahl), args.seed, skip)
        if not wanted:
            print("Keine passenden Seiten gefunden.")
            return 1
        out = export(conn, settings, Path(args.ziel), wanted, worker=worker, max_size=max_size,
                     parallel=max(1, args.parallel))
    finally:
        if worker is not None:
            worker.close()
        conn.close()
    print(f"{len(out)} Seite(n) nach {args.ziel} exportiert (Bilder, Tesseract-Text, seiten.json).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
