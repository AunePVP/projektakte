"""Document viewer (Dokument-Betrachter): how a file is shown inside the application window, and the
conversion of image formats browsers cannot show (TIFF, BMP) into a PNG or JPEG preview.

How a file is shown depends only on its extension (``view_kind``):

* ``pdf``     – the browser's own PDF viewer in an iframe of the original file route (same origin only).
* ``bild``    – JPEG, PNG, GIF, WebP: the original file in an ``<img>``.
* ``wandeln`` – TIFF, BMP: a preview converted on the server (``preview``), one page at a time.
* ``text``    – everything else (mails, Word, OpenDocument, text, and HTML/SVG, which must never run as
  active content in the application's origin): the cleaned text the index holds.

The conversion runs in the web process, not in the extraction worker: the worker reads one file at a
time for the indexing pass (up to minutes per file), and a person opening a plan must not wait behind it.
It is bounded instead: the file size, the decoded size (estimated from the header before anything is
decoded – a "decompression bomb" is refused before it costs memory), one conversion at a time, and the
preview is scaled to PREVIEW_EDGE pixels on its long side. Nothing is cached on disk or in the browser
(the application sends ``Cache-Control: no-store`` everywhere, so documents do not stay on shared PCs).
"""
from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from pathlib import Path

BROWSER_IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".jpe", ".gif", ".webp"})
CONVERT_IMAGE_EXT = frozenset({".tif", ".tiff", ".bmp", ".dib"})
# Files a browser would run (scripts in HTML/SVG/XML): never shown inline, only as text or as a download.
ACTIVE_EXT = frozenset({".html", ".htm", ".xhtml", ".xht", ".shtml", ".svg", ".svgz", ".xml", ".xsl", ".xslt",
                        ".js", ".mjs", ".mht", ".mhtml"})

MAX_FILE_BYTES = 300 * 1024 * 1024  # larger image files are not converted (download instead)
MAX_DECODED_BYTES = 320 * 1024 * 1024  # memory of one decoded page (an A0 plan scanned at 300 dpi: ~140 MB)
MAX_PIXELS = 200_000_000
MAX_PAGES = 999  # pages of a multi-page TIFF counted at most
PREVIEW_EDGE = 3200  # long side of the preview in pixels (enough to read a plan when zoomed)
WAIT_SECONDS = 20  # a request waits this long for a running conversion, then gets 503
_ONE_AT_A_TIME = threading.BoundedSemaphore(1)
# Memory per pixel of a decoded page including the copy a conversion for the preview makes (estimate).
_BYTES_PER_PIXEL = {"1": 2, "L": 1, "P": 5, "LA": 2, "La": 4, "PA": 6, "I;16": 10, "I;16B": 10, "I;16L": 10,
                    "I": 9, "F": 5, "RGB": 3, "YCbCr": 6, "LAB": 6, "HSV": 6, "RGBA": 4, "RGBa": 7, "RGBX": 7,
                    "CMYK": 4}
_ORIENTATION = {2: "FLIP_LEFT_RIGHT", 3: "ROTATE_180", 4: "FLIP_TOP_BOTTOM", 5: "TRANSPOSE", 6: "ROTATE_270",
                7: "TRANSVERSE", 8: "ROTATE_90"}


def view_kind(ext: str | None) -> str:
    ext = (ext or "").lower()
    if ext == ".pdf":
        return "pdf"
    if ext in BROWSER_IMAGE_EXT:
        return "bild"
    if ext in CONVERT_IMAGE_EXT:
        return "wandeln"
    return "text"


def is_active(ext: str | None) -> bool:
    return (ext or "").lower() in ACTIVE_EXT


class PreviewError(Exception):
    """The preview cannot be made; the message is German and shown to the person."""


class PreviewBusy(Exception):
    """Another conversion is running for longer than WAIT_SECONDS."""


@dataclass
class ImageInfo:
    width: int
    height: int
    pages: int


def _check(path: Path, img) -> None:
    width, height = img.size
    if width * height > MAX_PIXELS:
        raise PreviewError(f"Bild zu groß für die Vorschau ({width} × {height} Pixel).")
    decoded = width * height * _BYTES_PER_PIXEL.get(img.mode, 4)
    if decoded > MAX_DECODED_BYTES:
        raise PreviewError(f"Bild zu groß für die Vorschau ({width} × {height} Pixel).")


def _open(path: Path):
    """The image, opened lazily (only the header is read). Errors become PreviewError."""
    from PIL import Image, UnidentifiedImageError

    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise PreviewError("Datei zu groß für die Vorschau.")
        return Image.open(path)
    except PreviewError:
        raise
    except Image.DecompressionBombError as exc:
        raise PreviewError("Bild zu groß für die Vorschau.") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise PreviewError("Bild kann nicht gelesen werden.") from exc


def image_info(path: Path) -> ImageInfo:
    """Size of the first page and the number of pages (multi-page TIFF), from the headers only."""
    img = _open(path)
    with img:
        _check(path, img)
        try:
            pages = min(int(getattr(img, "n_frames", 1)), MAX_PAGES)
        except (EOFError, OSError, ValueError, SyntaxError, KeyError):  # a broken list of pages: the first only
            pages = 1
        return ImageInfo(img.size[0], img.size[1], max(1, pages))


def preview(path: Path, page: int = 1) -> tuple[bytes, str]:
    """Page ``page`` (from 1) of an image as PNG (black and white, grey) or JPEG (colour), scaled to
    PREVIEW_EDGE. Raises PreviewError (German message) or PreviewBusy."""
    if not _ONE_AT_A_TIME.acquire(timeout=WAIT_SECONDS):
        raise PreviewBusy()
    try:
        return _preview(path, page)
    finally:
        _ONE_AT_A_TIME.release()


def _preview(path: Path, page: int) -> tuple[bytes, str]:
    from PIL import Image

    img = _open(path)
    with img:
        try:
            if page > 1:
                if page > min(getattr(img, "n_frames", 1), MAX_PAGES):
                    raise PreviewError("Diese Seite gibt es nicht.")
                img.seek(page - 1)
            _check(path, img)  # every page has its own size
            try:
                turn = _ORIENTATION.get(int(img.getexif().get(0x0112) or 1))
            except Exception:  # noqa: BLE001 – a broken EXIF block: show the image as stored
                turn = None
            img.load()
            work = _displayable(img)
            factor = int(max(work.size) / PREVIEW_EDGE)
            if factor > 1:  # fast integer shrink first, the fine scaling below works on the small image
                work = work.reduce(factor)
            if turn:
                work = work.transpose(getattr(Image.Transpose, turn))
            work.thumbnail((PREVIEW_EDGE, PREVIEW_EDGE), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            if work.mode in ("L", "LA", "RGBA"):  # plans and scans: lossless and small; transparency kept
                work.save(buf, "PNG")
                return buf.getvalue(), "image/png"
            work.convert("RGB").save(buf, "JPEG", quality=85)
            return buf.getvalue(), "image/jpeg"
        except (PreviewError, MemoryError):
            raise
        except Image.DecompressionBombError as exc:
            raise PreviewError("Bild zu groß für die Vorschau.") from exc
        except (EOFError, OSError, ValueError, SyntaxError, KeyError) as exc:
            raise PreviewError("Bild kann nicht gelesen werden.") from exc


def _displayable(img):
    """The decoded page in a mode that can be shrunk and saved as PNG or JPEG: L, LA, RGB, RGBA or CMYK."""
    mode = img.mode
    if mode == "1":
        return img.convert("L")
    if mode == "P":
        return img.convert("RGBA" if "transparency" in img.info else "RGB")
    if mode.startswith("I;16") or mode == "I":
        return img.convert("I").point(lambda v: v * (1 / 256)).convert("L")  # 16 bit grey → 8 bit
    if mode == "F":
        return img.convert("L")
    if mode in ("L", "LA", "RGB", "RGBA", "CMYK"):
        return img
    return img.convert("RGBA" if mode in ("RGBa", "La", "PA") else "RGB")
