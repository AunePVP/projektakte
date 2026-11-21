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


_OLD = re.compile(r"^\d{3,}_.*\.(jpg|png|tif|webp|bin|tesseract\.txt)$")


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
    """Scale a page image down so that its width is at most ``max_size[0]`` and its height at most ``max_size[1]``,
    keeping its aspect ratio – never distorted, never turned, never enlarged (a landscape page with 1664x2048 is at
    most 1664 wide). Images that fit are returned byte for byte; a scaled image keeps its format (JPEG, PNG, TIFF,
    WebP), so its file name does not change."""
    if not max_size:
        return mime, data
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        max_w, max_h = max_size
        if w <= max_w and h <= max_h:
            return mime, data
        # whole-number arithmetic: the limiting side gets exactly its limit, the other is rounded down
        size = (max_w, max(1, h * max_w // w)) if max_w * h <= max_h * w else (max(1, w * max_h // h), max_h)
        small = img.convert("RGB" if img.mode not in ("RGB", "L") else img.mode).resize(size, Image.LANCZOS)
    buf = io.BytesIO()
    formats = {"image/jpeg": ("JPEG", {"quality": 90}), "image/tiff": ("TIFF", {"compression": "tiff_lzw"}),
               "image/webp": ("WEBP", {"quality": 90})}
    fmt, options = formats.get(mime, ("PNG", {"optimize": True}))
    small.save(buf, fmt, **options)
    return (mime if mime in formats else "image/png"), buf.getvalue()


def _thumb(data: bytes) -> bytes | None:
    """A 16x16 grey thumbnail, to tell whether two renderings show the same page."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as img:
            return img.convert("L").resize((16, 16), Image.BILINEAR).tobytes()
    except Exception:  # noqa: BLE001 – unreadable image: not comparable
        return None


def _difference(a: bytes | None, b: bytes | None) -> float:
    """Mean grey difference of two thumbnails (0 = same, 255 = opposite); 255 if one is missing."""
    if a is None or b is None:
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


SAME_PAGE = 12.0  # thumbnails closer than this show the same page (other resolution, other JPEG)


def _where(row) -> str:
    """The whole path of a document for messages (with the project folder), not only the path inside the project."""
    return printable(row["path"] or row["rel_path"])


def _render(conn, vp, worker, doc_id: int, pages: list[int], max_size) -> tuple[dict | None, list]:
    """(document row, [(page, mime, image, known text)]) of one document; nothing written."""
    row = conn.execute("SELECT id, project, path, rel_path, filename, ext, sha256, cache_path, mail_id "
                       "FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        return None, []
    data = vp._read(dict(row))
    if data is None:
        log.warning("%s nicht lesbar – übersprungen", _where(row))
        return None, []
    if row["ext"] in IMAGE_EXT:  # an image file goes to the model as it is
        from .extract import PageImages

        prepared = [PageImages(1, [(IMAGE_EXT[row["ext"]], data)])]
    else:
        prepared = extract_worker.page_images(row["filename"], data, sorted(pages), vp.prepare, worker=worker)
    done = []
    for p in prepared:
        if not p.images:
            log.info("%s Seite %d: kein Bild (%s)", _where(row), p.number, p.problem)
            continue
        mime, image = fit(*p.images[0], max_size)
        done.append((p.number, mime, image, _known_text(conn, doc_id, p.number)))
    return dict(row), done


def export(conn, settings, target: Path, wanted: list[tuple], *, worker=None,
           max_size: tuple[int, int] | None = None, parallel: int = 1, texts: bool = True) -> list[dict]:
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
            if texts:  # the text Projektakte has for the page (comparison in the correction tool)
                (target / f"{stem}.tesseract.txt").write_text(known, encoding="utf-8")
            entry = {"datei": f"{stem}{_EXT.get(mime, '.bin')}", "dokument": printable(row["rel_path"]),
                     "seite": number, "mime": mime, "tesseract_zeichen": len(known), "id": row["id"],
                     "pfad": _where(row)}
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


def _page_image(vp, worker, row: dict, pages: list[int], max_size) -> dict[int, tuple[str, bytes] | str]:
    """page -> (mime, image) as exported now, or the reason there is none."""
    data = vp._read(row)
    if data is None:
        return {n: "nicht gefunden oder nicht lesbar" for n in pages}
    if row["ext"] in IMAGE_EXT:  # an image file goes to the model as it is
        return {n: fit(IMAGE_EXT[row["ext"]], data, max_size) for n in pages}
    out: dict[int, tuple[str, bytes] | str] = {}
    for p in extract_worker.page_images(row["filename"], data, sorted(pages), vp.prepare, worker=worker):
        out[p.number] = fit(*p.images[0], max_size) if p.images else f"kein Bild ({p.problem})"
    return out


def renew(conn, settings, target: Path, *, worker=None, max_size: tuple[int, int] | None = None,
          mails: bool = True) -> dict:
    """Write the page images listed in ``target``/seiten.json again, as they are exported now (PDF pages and image
    files), under the same numbers and names – the same pages, nothing else: an export whose pages were already
    typed, corrected or transcribed keeps them. The document is found by its id (exports from 20.11.2026 on), else
    by its path inside the project; if several documents have that path, the one whose page looks like the old
    image is taken – or none, then the page is left as it was. Every new image is compared with the old one; pages
    that look different are counted (the file may have changed since). Texts and other files are left alone."""
    listing = target / "seiten.json"
    entries = json.loads(listing.read_text(encoding="utf-8"))
    vp = VisionPass(conn, None, projects_dir=getattr(settings, "projects_dir", None), worker=worker)
    cols = "id, project, path, rel_path, filename, ext, sha256, cache_path, mail_id"
    by_id = {r["id"]: dict(r) for r in conn.execute(f"SELECT {cols} FROM documents")}
    by_rel: dict[str, list[dict]] = {}
    for r in by_id.values():
        if mails or r["mail_id"] is None:
            by_rel.setdefault(printable(r["rel_path"]), []).append(r)
    stats = {"neu": 0, "unverändert": 0, "fehlt": 0, "mehrdeutig": 0, "abweichend": 0, "liste": []}
    groups: dict[tuple, list[dict]] = {}
    for entry in entries:
        key = ("id", entry["id"]) if entry.get("id") in by_id else ("pfad", entry.get("dokument", ""))
        groups.setdefault(key, []).append(entry)
    for (how, what), todo in groups.items():
        pages = sorted({int(e["seite"]) for e in todo})
        candidates = [by_id[what]] if how == "id" else by_rel.get(what, [])
        old = {e["datei"]: _thumb((target / e["datei"]).read_bytes()) if (target / e["datei"]).exists() else None
               for e in todo}
        rendered = {r["id"]: _page_image(vp, worker, r, pages, max_size) for r in candidates}
        chosen = None
        if len(candidates) == 1:
            chosen = candidates[0]
        elif len(candidates) > 1:  # same path in several projects: the one that looks like the old images
            def distance(r) -> float:
                worst = 0.0
                for e in todo:
                    got = rendered[r["id"]].get(int(e["seite"]))
                    worst = max(worst, _difference(old[e["datei"]], _thumb(got[1]) if isinstance(got, tuple) else None))
                return worst
            ranked = sorted(candidates, key=distance)
            if distance(ranked[0]) < SAME_PAGE <= distance(ranked[1]):
                chosen = ranked[0]
            else:
                log.warning("%s: %d Dokumente mit diesem Pfad, keines eindeutig gleich – %d Seite(n) bleiben",
                            what, len(candidates), len(todo))
                stats["mehrdeutig"] += len(todo)
                continue
        if chosen is None:
            log.warning("%s nicht mehr in Projektakte – %d Seite(n) bleiben", what, len(todo))
            stats["fehlt"] += len(todo)
            continue
        for entry in todo:
            got = rendered[chosen["id"]].get(int(entry["seite"]), "Seite nicht vorhanden")
            if isinstance(got, str):
                log.warning("%s Seite %s: %s – %s bleibt", _where(chosen), entry["seite"], got, entry["datei"])
                stats["fehlt"] += 1
                continue
            mime, image = got
            if _difference(old[entry["datei"]], _thumb(image)) >= SAME_PAGE:
                stats["abweichend"] += 1
                stats["liste"].append(entry["datei"])
                log.warning("%s sieht anders aus als vorher (%s Seite %s) – trotzdem neu geschrieben",
                            entry["datei"], _where(chosen), entry["seite"])
            old_file = target / entry["datei"]
            new_file = old_file.with_suffix(_EXT.get(mime, old_file.suffix))
            before = old_file.read_bytes() if old_file.exists() else None
            new_file.write_bytes(image)
            if new_file != old_file and old_file.exists():
                old_file.unlink()
            stats["neu" if before != image else "unverändert"] += 1
            entry.update(datei=new_file.name, mime=mime, id=chosen["id"], pfad=_where(chosen))
    listing.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    return stats


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
                    help="Seitenbilder höchstens so breit und so hoch, z. B. 1664x2048 – verkleinert mit gleichem "
                         "Seitenverhältnis (nie verzerrt, gedreht oder vergrößert; auch Querformat höchstens 1664 breit)")
    ap.add_argument("--dpi", type=int, help="PDF-Seiten mit höchstens so vielen dpi zeichnen (Voreinstellung: "
                                            "VISION_DPI, sonst 150); die Grenze aus --max-aufloesung gilt danach")
    ap.add_argument("--ohne-text", action="store_true",
                    help="keine ….tesseract.txt schreiben, nur Bilder und seiten.json (bei --alle immer)")
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
    if args.dpi is not None:  # read by the reading processes as well (extract.vision_dpi)
        if args.dpi < 1:
            ap.error("--dpi: eine positive ganze Zahl, z. B. 175")
        os.environ["VISION_DPI"] = str(args.dpi)
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
            stats = renew(conn, settings, Path(args.ziel), worker=worker, max_size=max_size,
                          mails=not args.ohne_mails)
            print(f"{stats['neu']} Seitenbild(er) in {args.ziel} neu geschrieben, {stats['unverändert']} unverändert"
                  f" (passten schon). Nicht erneuert: {stats['fehlt']} (Datei oder Seite fehlt), "
                  f"{stats['mehrdeutig']} (Pfad mehrdeutig). Anders aussehend als vorher: {stats['abweichend']}"
                  + (f" – {', '.join(stats['liste'][:10])}" if stats["liste"] else ""))
            return 0 if not (stats["fehlt"] or stats["mehrdeutig"]) else 1
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
                     parallel=max(1, args.parallel), texts=not (args.ohne_text or args.alle))
    finally:
        if worker is not None:
            worker.close()
        conn.close()
    what = "Bilder, seiten.json" if (args.ohne_text or args.alle) else "Bilder, Tesseract-Text, seiten.json"
    print(f"{len(out)} Seite(n) nach {args.ziel} exportiert ({what}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
