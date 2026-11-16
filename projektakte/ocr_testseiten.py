"""Export page images for an OCR model comparison (``scripts/ocr_vergleich.py``, run on another computer).

    python -m projektakte.ocr_testseiten --anzahl 50 --ziel /data/ocr-test
    python -m projektakte.ocr_testseiten --datei "DG-Ausbau_Teil 1" --seiten 49,56 --ziel /data/ocr-test

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
import random
import re
import sys
from pathlib import Path

from . import db, extract_worker
from .config import load_settings
from .documents import _in_folder, printable
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


def export(conn, settings, target: Path, wanted: list[tuple[int, int]], *, worker=None) -> list[dict]:
    target.mkdir(parents=True, exist_ok=True)
    if _clear(target):
        log.info("Seiten eines früheren Exports in %s entfernt", target)
    vp = VisionPass(conn, None, projects_dir=getattr(settings, "projects_dir", None), worker=worker)
    by_doc: dict[int, list[int]] = {}
    for doc_id, page in wanted:
        by_doc.setdefault(doc_id, []).append(page)
    out: list[dict] = []
    for doc_id, pages in by_doc.items():
        row = conn.execute("SELECT id, project, path, rel_path, filename, ext, sha256, cache_path, mail_id "
                           "FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            continue
        data = vp._read(dict(row))
        if data is None:
            log.warning("%s nicht lesbar – übersprungen", printable(row["rel_path"]))
            continue
        prepared = extract_worker.page_images(row["filename"], data, sorted(pages), vp.prepare, worker=worker)
        for p in prepared:
            if not p.images:
                log.info("%s Seite %d: kein Bild (%s)", printable(row["rel_path"]), p.number, p.problem)
                continue
            mime, image = p.images[0]
            stem = f"{len(out) + 1:03d}_{_safe(Path(row['filename']).stem)}_S{p.number}"
            (target / f"{stem}{_EXT.get(mime, '.bin')}").write_bytes(image)
            known = _known_text(conn, doc_id, p.number)
            (target / f"{stem}.tesseract.txt").write_text(known, encoding="utf-8")
            out.append({"datei": f"{stem}{_EXT.get(mime, '.bin')}", "dokument": printable(row["rel_path"]),
                        "seite": p.number, "mime": mime, "tesseract_zeichen": len(known)})
    (target / "seiten.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m projektakte.ocr_testseiten",
                                 description="Seitenbilder für einen Vergleich von OCR-Modellen exportieren")
    ap.add_argument("--ziel", default="/data/ocr-test", help="Zielordner (Voreinstellung: /data/ocr-test)")
    ap.add_argument("--anzahl", type=int, default=50, help="so viele wartende Scan-Seiten (Voreinstellung: 50)")
    ap.add_argument("--datei", help="stattdessen Seiten dieser Datei (Teil des Namens)")
    ap.add_argument("--seiten", default="", help="mit --datei: Seitennummern, z. B. 49,56 (sonst alle)")
    ap.add_argument("--seed", type=int, default=1, help="Zufallsauswahl wiederholbar (Voreinstellung: 1)")
    ap.add_argument("--ohne-ordner", default=",".join(DEFAULT_SKIP),
                    help="keine Seiten aus Ordnern dieses Namens, kommagetrennt (Voreinstellung: CAD,Bilder; "
                         "dazu SKIP_READ_FOLDERS und VISION_SKIP_FOLDERS); leer: alle")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    conn = db.connect(settings.db_path)
    worker = extract_worker.own_worker(settings)
    try:
        if args.datei:
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
        out = export(conn, settings, Path(args.ziel), wanted, worker=worker)
    finally:
        if worker is not None:
            worker.close()
        conn.close()
    print(f"{len(out)} Seite(n) nach {args.ziel} exportiert (Bilder, Tesseract-Text, seiten.json).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
