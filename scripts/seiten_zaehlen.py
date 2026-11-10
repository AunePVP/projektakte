"""Zählt die Seiten aller PDFs einer Ablage und wie viele davon die Bildauswertung brauchen.

Nur lesend, gibt nur Zahlen aus (keine Datei- oder Projektnamen). Aufruf im Repository:

    .venv/bin/python scripts/seiten_zaehlen.py /mnt/projekt                  # alle PDFs
    .venv/bin/python scripts/seiten_zaehlen.py /mnt/projekt --stichprobe 2000  # schnell hochgerechnet

Seitenarten:
- digital: Seite mit Textebene, ohne großes Bild – wird ohne Modell gelesen
- Scan mit OCR: Textebene und ein großes Bild (eingescannte Seite mit hinterlegtem Text) – geht in
  Variante A mit Bild und Text an das Modell (Handschrift, Stempel)
- Scan ohne Text: keine Textebene, aber ein Bild – das Modell liest alles
- leer/Vektor: weder Text noch Bild (Leerseite, CAD-Plan als Linien) – nichts für das Modell
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

BIG_IMAGE = 1_000_000  # pixels from which an image counts as a scanned page (about 1000 × 1000)
MIN_TEXT = 20  # characters from which a page has a text layer (as the app decides)
PICTURE_WORDS = ("bilder",)  # folders whose photos are left out (VISION_SKIP_FOLDERS=Bilder)


def _in_picture_folder(path: Path) -> bool:
    for part in path.parts[:-1]:
        name = part.casefold()
        if any(name == w or (name.endswith(w) and name[-len(w) - 1] in "-_ ") for w in PICTURE_WORDS):
            return True
    return False


def _largest_image(page) -> int:
    """Pixels of the largest image the page's resources name (also inside forms, one level)."""
    best = 0
    try:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") or {}
        for ref in list(xobjects.values())[:200]:
            obj = ref.get_object()
            sub = obj.get("/Subtype")
            if sub == "/Image":
                best = max(best, int(obj.get("/Width", 0)) * int(obj.get("/Height", 0)))
            elif sub == "/Form":
                inner = (obj.get("/Resources") or {}).get("/XObject") or {}
                for ref2 in list(inner.values())[:200]:
                    o2 = ref2.get_object()
                    if o2.get("/Subtype") == "/Image":
                        best = max(best, int(o2.get("/Width", 0)) * int(o2.get("/Height", 0)))
    except Exception:  # noqa: BLE001 – a broken resource dictionary: no image known
        pass
    return best


def count_pdf(path: str) -> Counter:
    logging.disable(logging.CRITICAL)  # pypdf warns a lot about broken files
    from pypdf import PdfReader

    c = Counter()
    try:
        if os.path.getsize(path) > 80 * 1024 * 1024:
            c["zu groß (über 80 MB, nicht gelesen)"] += 1
            return c
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            c["Dateien mit Passwort"] += 1
            return c
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                text = ""
            largest = _largest_image(page)
            big, has_image = largest >= BIG_IMAGE, largest > 0
            if len(text.strip()) >= MIN_TEXT:
                c["Seiten: Scan mit OCR" if big else "Seiten: digital"] += 1
            elif has_image:
                c["Seiten: Scan ohne Text"] += 1
            else:
                c["Seiten: leer/Vektor"] += 1
        c["PDFs gelesen"] += 1
    except Exception:  # noqa: BLE001
        c["PDFs nicht lesbar"] += 1
    return c


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Seiten der PDFs zählen (nur lesend, nur Zahlen).")
    ap.add_argument("pfad", type=Path)
    ap.add_argument("--stichprobe", type=int, default=0, metavar="N", help="nur N zufällige PDFs lesen und hochrechnen")
    ap.add_argument("--prozesse", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    args = ap.parse_args(argv)
    started = time.monotonic()
    pdfs, images, images_skipped = [], 0, 0
    for dirpath, dirnames, filenames in os.walk(args.pfad):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            ext = os.path.splitext(name)[1].lower()
            if ext == ".pdf":
                pdfs.append(os.path.join(dirpath, name))
            elif ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
                if _in_picture_folder(Path(dirpath) / name):
                    images_skipped += 1
                else:
                    images += 1
    total_pdfs = len(pdfs)
    sample = pdfs
    if args.stichprobe and args.stichprobe < total_pdfs:
        sample = random.Random(1).sample(pdfs, args.stichprobe)
    print(f"PDFs gefunden: {total_pdfs:,}".replace(",", "."), f"– gelesen werden {len(sample):,}".replace(",", "."),
          file=sys.stderr)
    counts = Counter()
    with ProcessPoolExecutor(args.prozesse) as pool:
        for i, c in enumerate(pool.map(count_pdf, sample, chunksize=8), start=1):
            counts.update(c)
            if i % 500 == 0:
                print(f"\r… {i:,} von {len(sample):,} PDFs".replace(",", "."), end="", file=sys.stderr, flush=True)
    print(file=sys.stderr)
    factor = total_pdfs / len(sample) if sample else 0

    def n(x: float) -> str:
        return f"{round(x):,}".replace(",", ".")

    print(f"PDFs auf der Ablage:            {n(total_pdfs)}" + (f" (Stichprobe {n(len(sample))}, hochgerechnet)"
                                                                 if factor != 1 else ""))
    for key in ("PDFs gelesen", "PDFs nicht lesbar", "Dateien mit Passwort", "zu groß (über 80 MB, nicht gelesen)"):
        if counts[key]:
            print(f"{key + ':':32}{n(counts[key] * factor)}")
    pages = {k: counts[k] * factor for k in ("Seiten: digital", "Seiten: Scan mit OCR", "Seiten: Scan ohne Text",
                                              "Seiten: leer/Vektor")}
    print(f"{'Seiten gesamt:':32}{n(sum(pages.values()))}")
    for k, v in pages.items():
        print(f"  {k[8:] + ':':30}{n(v)}")
    print(f"{'Bilder (ohne Bilder-Ordner):':32}{n(images)}")
    print(f"{'Bilder in Bilder-Ordnern:':32}{n(images_skipped)}")
    model = pages["Seiten: Scan mit OCR"] + pages["Seiten: Scan ohne Text"] + images
    print(f"{'für das Modell (Variante A):':32}{n(model)} Seiten/Bilder")
    for secs in (40, 60):
        days = model * secs / 86400
        print(f"  bei {secs} s je Seite, eine Anfrage gleichzeitig: {days:.0f} Tage; 4 gleichzeitig: {days / 4:.0f} Tage")
    print(f"(gezählt in {time.monotonic() - started:.0f} s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
