"""Reading the embedded images of scan pages, seen through Tesseract.

The model gets every PDF page rendered as a viewer shows it (extract._view); the embedded images are decoded and
put together only for Tesseract. Tests of that decoding (bounds against crafted PDFs, soft masks, strips, reduced
scale) read the pages here: ``reader.describe_image`` stands in for Tesseract and receives each image it would get,
and the result is built as for pages read right away (status, note, incomplete, retry)."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from projektakte import extract as extract_mod
from projektakte.extract import Extracted


def _mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def read_scans(path: Path | str, data: bytes, reader) -> Extracted:
    """The pages of a PDF without a text layer, their images read by ``reader`` (a fake model: ``describe_image``)
    in place of Tesseract."""
    name = Path(path).name

    def ocr(image: bytes) -> str:
        return reader.describe_image(image, _mime(image), "").text or ""

    with mock.patch.object(extract_mod, "ocr_image", ocr), extract_mod._pdf_limits():
        pdf = extract_mod._open_pdf(data)
        doc = extract_mod._DocState(source=data)
        items = list(enumerate(pdf.pages, start=1))  # all pages, as pages without a text layer
        scan = extract_mod._read_scanned_pages(items, None, name, ocr=True, doc=doc)
    unread = scan.unread
    if scan.pages:
        notes = [f"{unread} Seite(n) ohne Textebene nicht gelesen ({scan.reason()})"] if unread else []
        if scan.partial:
            notes.append(scan.partial_note())
        return Extracted(sorted(scan.pages, key=lambda p: p.number or 0), message="; ".join(notes) or None,
                         incomplete=bool(unread or scan.partial), retry=scan.retry)
    if not unread:
        return Extracted([], status="empty", message="kein Text gefunden")
    return Extracted([], status="error" if scan.retry else "skipped", message=f"Scan ohne Textebene – {scan.reason()}",
                     incomplete=True, retry=scan.retry)


__all__ = ["read_scans"]
