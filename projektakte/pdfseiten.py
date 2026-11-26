"""PDF pages read with PDFium alone – the engine of Chrome's PDF viewer (pypdfium2): the text of a page, the place
of its lines, whether it is a scan, and its image. One engine for text and image: what is read is what the page
image shows, and the place of a line is its place on that image.

- :func:`read`: every page with its text as a viewer's text selection gives it (words are not torn apart where a
  line is written in several runs – justified text), its lines as :class:`Segment` with a box relative to the
  rendered page (0–1, origin top left, turned by /Rotate, inside the crop box – as the boxes of the page model),
  and whether the page is a scan (page images over most of it: its text, if any, is an OCR layer).
- :func:`render`: pages as a viewer shows them, for the page model (VISION_DPI, at most MAX_PIXELS).

Runs in the extraction worker (untrusted files, see extract_worker.py). Nothing here decodes embedded images or
walks content streams itself.
"""
from __future__ import annotations

import ctypes
import io
import logging
import os
import time
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger("projektakte.pdfseiten")

MIN_TEXT = 20  # a page with fewer characters has no text layer to speak of
MAX_PAGES = 5000  # pages read of one file (a file with more is cut there and marked incomplete)
MAX_CHARS_PAGE = 200_000  # characters read of one page (a plan with every label as text stays below)
MAX_PIXELS = 16_000_000  # a page image at most (A1 at 150 dpi is below; larger pages at a lower resolution)
RENDER_SECONDS = 20.0  # wall time for rendering one page
BLANK_LEVEL = 200  # a rendered page without any pixel darker than this (0 black … 255 white) is blank
DPI_DEFAULT = 150
BLANK = "Seite leer"
NOT_THERE = "Seite nicht vorhanden"
RENDER_FAILED = "Seite nicht darstellbar"
# a page is a scan if the images it draws cover this share of it and have this many pixels together
SCAN_COVERAGE = 0.5
SCAN_PIXELS = 1_000_000
SOFT_HYPHEN = "\u00ad"  # where PDFium found a word divided at the end of a line
DIVIDED = ("\x02", "\ufffe")  # what PDFium puts where a word is divided at a line end
_GAP_HEIGHTS = 2.5  # a gap this many line heights wide inside a line: another column, a piece of its own
_SCALE = 100_000  # device size for PDFium's page→device mapping (integers): boxes exact to 1/100000


@dataclass
class Segment:
    """A line of text (or the part of a line in one column) and where it is on the rendered page."""
    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    def as_list(self) -> list:
        return [round(self.x0, 4), round(self.y0, 4), round(self.x1, 4), round(self.y1, 4), self.text]


@dataclass
class PdfPage:
    number: int
    text: str = ""  # its lines, "\n" between them; "" without a usable text layer
    segments: list[Segment] = field(default_factory=list)
    scan: bool = False  # page images over most of the page: its text is an OCR layer, not the page's own
    width: float = 0.0  # points, as displayed
    height: float = 0.0


@dataclass
class PdfRead:
    pages: list[PdfPage] = field(default_factory=list)
    count: int = 0  # pages of the file (more than ``pages`` beyond MAX_PAGES)
    problem: str | None = None  # why the file could not be read
    encrypted: bool = False  # it needs a password


def usable(text: str) -> bool:
    """Is this a text layer a reader can use: at least MIN_TEXT characters, not mostly replacement, private-use
    or control characters (fonts without a usable character map)?"""
    chars = [c for c in text if not c.isspace()]
    if len(text.strip()) < MIN_TEXT:
        return False
    odd = sum(1 for c in chars if c == "\ufffd" or unicodedata.category(c) in ("Cc", "Co", "Cn", "Cs"))
    return odd * 2 <= len(chars)


def dpi() -> int:
    """VISION_DPI from the environment: resolution of the page images for the model (whole number, 0 or unset:
    DPI_DEFAULT; the page model sets 175, config.apply_vision_env)."""
    raw = (os.environ.get("VISION_DPI") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DPI_DEFAULT
    return value if value > 0 else DPI_DEFAULT


def _tick() -> None:
    """Between pages: the worker's check (it ends if the app has gone) and its sign of life."""
    from . import extract

    extract._check_page()
    extract._progress()


def _open(data: bytes):
    """(document, problem, encrypted). PDFium opens files protected against editing only (empty password) and
    repairs damaged ones the way viewers do."""
    import pypdfium2 as pdfium

    try:
        return pdfium.PdfDocument(data), None, False
    except Exception as exc:  # noqa: BLE001 – not a PDF, cut off, or password-protected
        text = str(exc)
        if "assword" in text:
            return None, text, True
        return None, f"Datei nicht lesbar ({type(exc).__name__}: {text})"[:300], False


# -- text ----------------------------------------------------------------------------------------------------
def read(data: bytes, name: str = "") -> PdfRead:
    """Every page of the file (at most MAX_PAGES) with its text, lines and whether it is a scan."""
    doc, problem, encrypted = _open(data)
    if doc is None:
        return PdfRead(problem=problem, encrypted=encrypted)
    try:
        count = len(doc)
        result = PdfRead(count=count)
        for index in range(min(count, MAX_PAGES)):
            _tick()
            try:
                result.pages.append(_read_page(doc, index))
            except MemoryError:
                raise
            except Exception as exc:  # noqa: BLE001 – one broken page: without text, the model still sees it
                log.warning("%s Seite %d: Text nicht lesbar (%s)", name, index + 1, exc)
                result.pages.append(PdfPage(index + 1))
        return result
    finally:
        doc.close()


def _read_page(doc, index: int) -> PdfPage:
    page = doc[index]
    try:
        width, height = page.get_size()
        out = PdfPage(index + 1, width=width, height=height)
        textpage = page.get_textpage()
        try:
            segments = _segments(page, textpage, width, height)
        finally:
            textpage.close()
        text = "\n".join(s.text for s in _lines(segments))
        if usable(text):
            out.text, out.segments = text, segments
        out.scan = _is_scan(page, width, height)
        return out
    finally:
        page.close()


def _lines(segments: list[Segment]) -> list[Segment]:
    """The pieces of one line (columns) as one line again, for the page's plain text."""
    lines: list[Segment] = []
    for s in segments:
        last = lines[-1] if lines else None
        if last is not None and getattr(s, "_line", None) == getattr(last, "_line", object()):
            lines[-1] = joined = Segment(min(last.x0, s.x0), min(last.y0, s.y0), max(last.x1, s.x1),
                                         max(last.y1, s.y1), f"{last.text} {s.text}")
            joined._line = s._line  # type: ignore[attr-defined]
        else:
            lines.append(s)
    return lines


def _segments(page, textpage, width: float, height: float) -> list[Segment]:
    """The lines of the page in PDFium's order, a line with a wide gap (columns) as several pieces. A word
    divided at the end of a line is joined with the next line's beginning; a hyphen before a capital stays."""
    import pypdfium2.raw as raw

    tp = textpage.raw
    count = min(raw.FPDFText_CountChars(tp), MAX_CHARS_PAGE)
    left, right, bottom, top = (ctypes.c_double() for _ in range(4))
    pieces: list[tuple[int, list[str], list[float]]] = []  # (line number, characters, [l, b, r, t] in page units)
    line = 0
    chars: list[str] = []
    box: list[float] | None = None
    last_right: float | None = None

    def close() -> None:
        nonlocal chars, box, last_right
        text = "".join(chars).strip()
        if text and box is not None:
            pieces.append((line, [text], box))
        chars, box, last_right = [], None, None

    for i in range(count):
        code = raw.FPDFText_GetUnicode(tp, i)
        ch = chr(code) if 0 < code < 0x110000 and not 0xD800 <= code <= 0xDFFF else ""
        if ch in ("\r", "\n"):
            if ch == "\n" or (chars and box is not None):
                close()
            if ch == "\n":
                line += 1
            continue
        if ch in DIVIDED:  # PDFium: a word divided at the end of a line (it leaves the line break out)
            chars.append(SOFT_HYPHEN)
            close()
            line += 1
            continue
        if ch in ("-", SOFT_HYPHEN) and raw.FPDFText_IsHyphen(tp, i) == 1 and _ends_line(raw, tp, i, count):
            chars.append(SOFT_HYPHEN)  # the word goes on in the next line
            continue
        if not ch or (ord(ch) < 32 and ch != "\t"):
            continue
        if raw.FPDFText_IsGenerated(tp, i) == 1 or ch.isspace():
            chars.append(" ")
            continue
        if raw.FPDFText_GetCharBox(tp, i, ctypes.byref(left), ctypes.byref(right), ctypes.byref(bottom),
                                   ctypes.byref(top)):
            l, r, b, t = left.value, right.value, bottom.value, top.value
            tall = max(t - b, 1.0)
            if box is not None and last_right is not None and l - last_right > _GAP_HEIGHTS * max(tall, box[3] - box[1]):
                close()  # another column on the same line
            if box is None:
                box = [l, b, r, t]
            else:
                box = [min(box[0], l), min(box[1], b), max(box[2], r), max(box[3], t)]
            last_right = max(r, last_right) if last_right is not None else r
        chars.append(ch)
    close()

    segments: list[Segment] = []
    for number, text, (l, b, r, t) in pieces:
        x0, y0 = _to_page_image(raw, page, l, t)
        x1, y1 = _to_page_image(raw, page, r, b)
        seg = Segment(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), text[0])
        seg._line = number  # type: ignore[attr-defined]
        segments.append(seg)
    return _join_divided_words(segments)


def _ends_line(raw, tp, i: int, count: int) -> bool:
    """Is the hyphen at ``i`` the last character of its line (only line breaks or the end follow)?"""
    j = i + 1
    if j >= count:
        return False
    return chr(raw.FPDFText_GetUnicode(tp, j)) in ("\r", "\n")


def _join_divided_words(segments: list[Segment]) -> list[Segment]:
    """A line ending with a divided word takes the rest of the word from the next line: "Böschungs" + "oberkanten
    gilt" → "Böschungsoberkanten" / "gilt". Before a capital or a digit the hyphen stays ("Brandschutz-" /
    "Dienststelle"), and the lines stay as they are."""
    for k, seg in enumerate(segments):
        if not seg.text.endswith(SOFT_HYPHEN):
            seg.text = seg.text.replace(SOFT_HYPHEN, "-")
            continue
        head = seg.text[:-1].replace(SOFT_HYPHEN, "-")
        nxt = segments[k + 1] if k + 1 < len(segments) else None
        if nxt is None or not nxt.text or not nxt.text[0].islower():
            seg.text = head + "-"
            continue
        rest, _, tail = nxt.text.partition(" ")
        seg.text = head + rest
        nxt.text = tail
    return [s for s in segments if s.text]


def _to_page_image(raw, page, x: float, y: float) -> tuple[float, float]:
    """A point of the page (PDF units) relative to the page as rendered (0–1 from the top left corner)."""
    dx, dy = ctypes.c_int(), ctypes.c_int()
    raw.FPDF_PageToDevice(page.raw, 0, 0, _SCALE, _SCALE, 0, x, y, ctypes.byref(dx), ctypes.byref(dy))
    return min(1.0, max(0.0, dx.value / _SCALE)), min(1.0, max(0.0, dy.value / _SCALE))


def _is_scan(page, width: float, height: float) -> bool:
    """Do the images the page draws cover SCAN_COVERAGE of it with SCAN_PIXELS together (strips and tiles count
    together)? Then the page is a scan and its text an OCR layer. A logo or a photo on a digital page is not."""
    import pypdfium2.raw as raw

    area = width * height
    if area <= 0:
        return False
    covered, pixels = 0.0, 0
    try:
        for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=4):
            try:
                l, b, r, t = obj.get_bounds()
                covered += max(0.0, min(r, max(width, height)) - max(l, 0.0)) * max(0.0, min(t, max(width, height)) - max(b, 0.0))
                w, h = obj.get_px_size()
                pixels += int(w) * int(h)
            except Exception:  # noqa: BLE001 – one unreadable image object
                continue
    except Exception as exc:  # noqa: BLE001 – objects not listable: counted as a digital page
        log.debug("Bilder der Seite nicht bestimmbar (%s)", exc)
        return False
    return pixels >= SCAN_PIXELS and covered >= SCAN_COVERAGE * area


# -- images --------------------------------------------------------------------------------------------------
def render(data: bytes, numbers: list[int], name: str = "") -> list:
    """The given pages as a viewer shows them (crop box, /Rotate, annotations, filled-in form fields), in colour
    at VISION_DPI, at most MAX_PIXELS: a list of extract.PageImages – with the image, or why there is none
    (BLANK for a page without anything darker than BLANK_LEVEL)."""
    from .extract import PageImages

    doc, problem, _encrypted = _open(data)
    if doc is None:
        return [PageImages(n, problem=problem or RENDER_FAILED) for n in numbers]
    try:
        try:  # form fields are drawn as a viewer shows them (before any page is opened)
            doc.init_forms()
        except Exception as exc:  # noqa: BLE001 – a broken form: the pages without their fields
            log.debug("%s: Formularfelder nicht lesbar (%s)", name, exc)
        count = len(doc)
        out = []
        for n in numbers:
            _tick()
            if not 1 <= n <= count:
                out.append(PageImages(n, problem=NOT_THERE))
                continue
            try:
                out.append(_render(doc, n))
            except MemoryError:
                from .extract import _memory_bound, _out_of_memory

                _out_of_memory(MemoryError())
                out.append(PageImages(n, problem=f"Seite zu groß zum Zeichnen ({_memory_bound()})"))
        return out
    finally:
        doc.close()


def _render(doc, number: int):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    from .extract import PageImages

    page = None
    try:
        page = doc[number - 1]
        w_pt, h_pt = page.get_size()
        scale = dpi() / 72
        if w_pt * h_pt * scale * scale > MAX_PIXELS:
            scale = (MAX_PIXELS / (w_pt * h_pt)) ** 0.5
        width, height = max(1, int(w_pt * scale)), max(1, int(h_pt * scale))
        bitmap = pdfium.PdfBitmap.new_native(width, height, raw.FPDFBitmap_BGR)
        raw.FPDFBitmap_FillRect(bitmap, 0, 0, width, height, 0xFFFFFFFF)
        deadline = time.monotonic() + RENDER_SECONDS
        need_pause = raw.IFSDK_PAUSE._fields_[1][1](lambda _pause: time.monotonic() > deadline)
        pause = raw.IFSDK_PAUSE(version=1, NeedToPauseNow=need_pause)
        flags = raw.FPDF_ANNOT
        status = raw.FPDF_RenderPageBitmap_Start(bitmap, page, 0, 0, width, height, 0, flags, ctypes.byref(pause))
        while status == raw.FPDF_RENDER_TOBECONTINUED and time.monotonic() <= deadline:
            status = raw.FPDF_RenderPage_Continue(page, ctypes.byref(pause))
        raw.FPDF_RenderPage_Close(page)
        if status != raw.FPDF_RENDER_DONE:
            late = status == raw.FPDF_RENDER_TOBECONTINUED
            return PageImages(number, problem=f"Seite zu aufwendig (über {RENDER_SECONDS:.0f} s zum Zeichnen)" if late
                              else RENDER_FAILED)
        if doc.formenv:  # filled-in form fields, over the page
            raw.FPDF_FFLDraw(doc.formenv, bitmap, page, 0, 0, width, height, 0, flags)
        img = bitmap.to_pil()
        if img.convert("L").getextrema()[0] > BLANK_LEVEL:
            return PageImages(number, problem=BLANK)
        return PageImages(number, [_encoded(img)])
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 – a page PDFium cannot open or render
        log.debug("Seite %d nicht darstellbar (%s)", number, exc)
        return PageImages(number, problem=RENDER_FAILED)
    finally:
        if page is not None:
            page.close()


MIN_SIDE = 32  # a narrower image is put onto white of this size (vision models cut images into patches)


def _encoded(img) -> tuple[str, bytes]:
    """The page as JPEG (mime, bytes), at least MIN_SIDE pixels on both sides."""
    from PIL import Image

    if img.mode != "RGB":
        img = img.convert("RGB")
    if min(img.size) < MIN_SIDE:
        base = Image.new("RGB", (max(img.size[0], MIN_SIDE), max(img.size[1], MIN_SIDE)), "white")
        base.paste(img, (0, 0))
        img = base
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return "image/jpeg", out.getvalue()
