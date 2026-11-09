"""Text extraction: classic extraction first, the multimodal model only as a fallback for scans
and plans (and only with LLM_VISION=1). Without vision, such files are recorded as skipped and
marked incomplete, so they are read once vision is switched on (see documents.reprocess_due)."""
from __future__ import annotations

import codecs
import email
import hashlib
import io
import logging
import math
import re
from dataclasses import dataclass, field
from email import policy
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("projektakte.extract")

TEXT_EXT = {".txt", ".text", ".md", ".markdown", ".csv", ".log"}
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp"}
SUPPORTED_EXT = TEXT_EXT | {".pdf", ".docx", ".eml"} | set(IMAGE_EXT)
VISION_PROMPT = (
    "Transkribiere den gesamten lesbaren Text dieses gescannten Dokuments bzw. Plans auf Deutsch. "
    "Beschreibe bei Plänen kurz Inhalt, Beschriftungen und Brandschutz-Symbole. Erfinde nichts."
)
# Bump when the extraction itself changes in a way that could read more of incomplete documents.
# 2: images inside PDFs are decoded robustly (Pillow); pages whose images could not be decoded are
#    retried instead of being recorded as "no text" – documents stored by version 1 are read again.
# 3: pages with more images than MAX_IMAGES_PER_PAGE (strip scans) are put together into one page
#    image; pages read only partly are marked incomplete – documents of version 2 are read again.
# 4: an image drawn several times is decoded and sent once; per-page limits on the declared image
#    sizes before decoding – incomplete documents of version 3 are read again.
# 5: images are told apart by their PDF object (one object under several names or in several forms is
#    one image, also across the pages of a document); the limits use the real size of JPEG & co.; the
#    content walk and each document are bounded; inline images inside forms are read; large JPEGs are
#    decoded at reduced scale – incomplete documents of version 4 are read again (e.g. a page with "184
#    Bild(er) nicht ausgewertet" that holds one image under many names).
EXTRACT_VERSION = 5
# Images of one PDF page that are sent to the model one by one (scanners write one image per page).
# A page with more images – scanners and PDF tools that cut a page into strips or tiles – is put
# together into one image at the images' positions on the page and sent as that.
MAX_IMAGES_PER_PAGE = 3
# Larger page images are scaled down before they are sent (an A0 plan scanned at 300 dpi has about
# 140 megapixels; multimodal models work with far fewer and requests must stay reasonably small).
MAX_IMAGE_PIXELS = 16_000_000
# Bounds for reading scan pages, all checked *before* the work they bound: a tiny PDF can draw one large
# image under hundreds of names, nest form XObjects so that their drawings multiply, declare a small
# image whose data is huge, or repeat a page thousands of times. Without bounds that exhausts memory and
# CPU of the app process – mail attachments are read there automatically, in the indexer thread.
# Exceeding a bound leaves the rest out with a note (the document stays incomplete, without retry: the
# file will not change); it never stops indexing.
MAX_PAGE_IMAGES = 200  # different images (PDF objects) of one page that are decoded
MAX_PAGE_PLACEMENTS = 2000  # different positions at which they are drawn (followed in the content)
MAX_PAGE_OPERATIONS = 1_000_000  # content operations walked per page (a form's each time it is drawn)
MAX_PAGE_FORMS = 1000  # form XObjects followed per page (each drawing counts)
# Decoded content (page and form streams) parsed per page and per document: pypdf parses a content stream
# completely before the walk can stop (about 1 s per MB), and a few KB of compressed content can decode
# to megabytes. A scan page has well under a kilobyte.
MAX_PAGE_CONTENT_BYTES = 4_000_000
MAX_DOC_CONTENT_BYTES = 32_000_000
# Pixels of all images decoded for one page, counted with the larger of declared and real size (JPEG,
# JPEG 2000, CCITT and JBIG2 data state their own size, which need not match /Width and /Height; it is
# read from the header without decoding). 150 MP keeps an A0 plan at 300 dpi (140 MP) readable. Since
# large JPEGs and JPEG 2000 images are decoded at reduced scale (never far beyond the MAX_IMAGE_PIXELS
# they are sent at), this limit no longer bounds their memory but still the CPU time (every coefficient
# is read); for the formats that can only be decoded in full (Flate, CCITT, JBIG2) it bounds the memory
# of one page (about 150 MB bilevel/grey; pypdf itself refuses raw samples above 75 MB, 25 MP in colour).
MAX_PAGE_PIXELS = 150_000_000
# Bounds per document: the same image (PDF object) is decoded and sent once per document and its
# transcript reused on every page that draws it; beyond these, pages are left out with a note. 500 model
# calls and 4000 MP cover a scanned file of several hundred A4 pages at 300 dpi.
MAX_DOC_VISION_CALLS = 500
MAX_DOC_PIXELS = 4_000_000_000
# Each decoding is charged at least this many pixels against MAX_DOC_PIXELS (a decoding has a fixed
# cost, JBIG2 even starts a program): bounds documents made of thousands of tiny images.
_MIN_DECODE_CHARGE = 250_000
# Putting a page together may paste and rescale at most this multiple of MAX_IMAGE_PIXELS (strips and
# tiles cover the page about once; heavily overlapping placements are no strip or tile scan).
_COMPOSE_WORK_FACTOR = 8
_SENDABLE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def extraction_profile(llm) -> str:
    """What the extraction can read with this model: without vision the LLM plays no part.

    Stored per document; incomplete documents are processed again when the profile changes
    (e.g. LLM_VISION switched on, or another multimodal model)."""
    if llm is not None and getattr(llm, "vision", False):
        return f"v{EXTRACT_VERSION}:vision:{getattr(llm, 'model', '') or '?'}"
    return f"v{EXTRACT_VERSION}:text"


@dataclass
class Page:
    number: int | None
    text: str


@dataclass
class Extracted:
    pages: list[Page] = field(default_factory=list)
    status: str = "ok"  # ok | empty | skipped | error
    message: str | None = None
    method: str = "text"  # text | vision
    # part of the content could not be read with this extraction profile (scan without vision,
    # failed or empty model answer, error) – another profile may read more
    incomplete: bool = False
    # a (possibly transient) failure occurred – try again later even with the same profile
    retry: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)


_BOMS = (  # UTF-32 first: its little-endian BOM starts with the UTF-16 one
    (codecs.BOM_UTF32_LE, "utf-32-le"), (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _utf16_without_bom(data: bytes) -> str | None:
    """Guess BOM-less UTF-16 (Windows tools): text in Latin script has a zero byte in every other
    position. Returns the codec name or None."""
    sample = data[:4096]
    pairs = len(sample) // 2
    if pairs < 2:
        return None
    zeros_even = sample[0 : pairs * 2 : 2].count(0) / pairs
    zeros_odd = sample[1 : pairs * 2 : 2].count(0) / pairs
    if zeros_odd > 0.4 and zeros_even < 0.05:
        return "utf-16-le"
    if zeros_even > 0.4 and zeros_odd < 0.05:
        return "utf-16-be"
    return None


def decode_text(data: bytes) -> str:
    """Decode a text file: BOM (UTF-8/16/32), BOM-less UTF-16, UTF-8, then Windows-1252."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data[len(bom):].decode(enc, "replace")
    enc16 = _utf16_without_bom(data)
    if enc16:  # strong evidence – a truncated last byte must not turn it into cp1252 garbage
        return data.decode(enc16, "replace")
    for enc in ("utf-8", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


# Declared charsets that say nothing: many mail programs write "us-ascii" for 8-bit text.
_NO_CHARSET = {None, "", "us-ascii", "ascii", "ansi_x3.4-1968", "unknown-8bit", "x-unknown"}


def mime_text(part) -> str:
    """Text of a MIME text part (mail body). Without a charset parameter – or with one that cannot
    be right ("us-ascii" for 8-bit text) – the transfer-decoded bytes are decoded like a text file
    (UTF-8 first, then Windows-1252): the email package would decode them as ASCII and replace every
    umlaut. A declared charset is used as it is; an unknown or wrong one falls back the same way."""
    charset = (part.get_content_charset() or "").strip().lower() or None
    raw = part.get_payload(decode=True) or b""
    if isinstance(raw, str):  # defensive: some broken parts hand out str
        raw = raw.encode("utf-8", "surrogateescape")
    if charset in _NO_CHARSET and (charset is None or any(b > 0x7F for b in raw)):
        return decode_text(raw)
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        return decode_text(raw)


class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    p.feed(html)
    text = "".join(p.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _pdf(data: bytes, llm, name: str) -> Extracted:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    empty_pages = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 – broken pages must not stop indexing
            log.warning("%s Seite %d: Text nicht lesbar (%s)", name, i, exc)
            text = ""
        if len(text.strip()) < 20:
            empty_pages.append((i, page))
        else:
            pages.append(Page(i, text))
    if not empty_pages:
        return Extracted(pages)
    if llm is None or not getattr(llm, "vision", False):
        # vision off: the pages may well be scans – a later profile with vision reads them
        if pages:
            return Extracted(pages, incomplete=True, message=(
                f"{len(empty_pages)} Seite(n) ohne Textebene – Bildauswertung ausgeschaltet (LLM_VISION=0)"))
        return Extracted([], status="skipped", incomplete=True,
                         message="Scan ohne Textebene – Bildauswertung ausgeschaltet (LLM_VISION=0)")
    scan = _read_scanned_pages(empty_pages, llm, name)
    pages = sorted(pages + scan.pages, key=lambda p: p.number or 0)
    method = "vision" if scan.pages else "text"
    unread = scan.unread
    if pages:
        notes = [f"{unread} Seite(n) ohne Textebene nicht gelesen ({scan.reason()})"] if unread else []
        if scan.partial:
            notes.append(scan.partial_note())
        return Extracted(pages, method=method, message="; ".join(notes) or None,
                         incomplete=bool(unread or scan.partial), retry=scan.retry)
    if not unread:  # only blank pages – nothing a model could read
        return Extracted([], status="empty", message="kein Text gefunden")
    return Extracted([], status="error" if scan.retry else "skipped",
                     message=f"Scan ohne Textebene – {scan.reason()}", incomplete=True, retry=scan.retry)


def _n(number: int) -> str:
    """A number with German thousands separators."""
    return f"{number:,}".replace(",", ".")


def _page_bounds() -> str:
    return (f"mehr als {_n(MAX_PAGE_CONTENT_BYTES // 1_000_000)} MB Inhalt, {_n(MAX_PAGE_OPERATIONS)} "
            f"Zeichenbefehle, {_n(MAX_PAGE_FORMS)} Formularaufrufe bzw. {_n(MAX_PAGE_PLACEMENTS)} Bildpositionen "
            f"je Seite oder {_n(MAX_DOC_CONTENT_BYTES // 1_000_000)} MB Inhalt je Dokument")


def _doc_bounds() -> str:
    return (f"mehr als {_n(MAX_DOC_VISION_CALLS)} Bildauswertungen bzw. {_n(MAX_DOC_PIXELS // 1_000_000)} "
            "Megapixel je Dokument")


@dataclass
class _ScanResult:
    """Outcome of reading the pages without a text layer with the multimodal model."""
    pages: list[Page] = field(default_factory=list)
    no_text: int = 0  # the model read the page images but answered nothing
    undecodable: int = 0  # page images could not be decoded (Pillow/jbig2dec missing, broken image)
    model_failed: int = 0  # the model call failed (timeout, server down)
    too_large: int = 0  # all images of the page over MAX_PAGE_PIXELS (checked before decoding)
    too_complex: int = 0  # the content walk hit a page bound before it found an image
    doc_limit: int = 0  # left out: the document's bounds (MAX_DOC_*) were used up
    decode_error: str = ""  # first decoding problem, for the message
    # pages read only partly: some of their images could not be decoded, their model call failed, they
    # could not be put together into one page image and were left out (counts of images), or the page's
    # content was not walked to its end (count of pages)
    partial: int = 0
    partial_undecodable: int = 0
    partial_failed: int = 0
    partial_left_out: int = 0
    partial_over_limit: int = 0
    partial_doc_limit: int = 0
    partial_truncated: int = 0

    @property
    def unread(self) -> int:
        return (self.no_text + self.undecodable + self.model_failed + self.too_large + self.too_complex
                + self.doc_limit)

    @property
    def retry(self) -> bool:
        """A failure that may go away (model back, missing component installed): try again later."""
        return bool(self.undecodable or self.model_failed or self.partial_undecodable or self.partial_failed)

    def partial_note(self) -> str:
        """German note on the pages that were read only partly."""
        parts = []
        if self.partial_undecodable:
            why = f": {self.decode_error}" if self.decode_error else ""
            parts.append(f"{self.partial_undecodable} Bild(er) nicht lesbar{why}")
        if self.partial_failed:
            parts.append(f"{self.partial_failed} Bild(er): Bildauswertung fehlgeschlagen")
        if self.partial_left_out:
            parts.append(f"{self.partial_left_out} Bild(er) nicht ausgewertet (mehr als {MAX_IMAGES_PER_PAGE} "
                         "Bilder je Seite, nicht zu einem Seitenbild zusammensetzbar)")
        if self.partial_over_limit:
            parts.append(f"{self.partial_over_limit} Bild(er) nicht ausgewertet (mehr als {MAX_PAGE_IMAGES} "
                         f"Bilder bzw. {MAX_PAGE_PIXELS // 1_000_000} Megapixel je Seite)")
        if self.partial_doc_limit:
            parts.append(f"{self.partial_doc_limit} Bild(er) nicht ausgewertet ({_doc_bounds()})")
        if self.partial_truncated:
            parts.append(f"Inhalt von {self.partial_truncated} Seite(n) nicht vollständig durchsucht "
                         f"({_page_bounds()})")
        return f"{self.partial} Seite(n) nur teilweise gelesen ({'; '.join(parts)})"

    def reason(self) -> str:
        reasons = []  # (pages, text)
        if self.undecodable:
            reasons.append((self.undecodable, f"Bilder nicht lesbar: {self.decode_error}" if self.decode_error
                            else "Bilder nicht lesbar"))
        if self.model_failed:
            reasons.append((self.model_failed, "Bildauswertung fehlgeschlagen"))
        if self.no_text:
            reasons.append((self.no_text, "Bildauswertung ergab keinen Text"))
        if self.too_large:
            reasons.append((self.too_large,
                            f"Bilder zu groß (über {MAX_PAGE_PIXELS // 1_000_000} Megapixel je Seite)"))
        if self.too_complex:
            reasons.append((self.too_complex, f"Seiteninhalt zu umfangreich ({_page_bounds()})"))
        if self.doc_limit:
            reasons.append((self.doc_limit, f"Dokument zu umfangreich ({_doc_bounds()})"))
        if len(reasons) > 1:  # say which reason applies to how many pages
            return "; ".join(f"{n}× {text}" for n, text in reasons)
        return "; ".join(text for _n_pages, text in reasons)


_pillow_warned = False


def _decode_problem(exc: Exception) -> str:
    """Short German description of why a PDF image could not be decoded."""
    global _pillow_warned
    text = str(exc)
    if isinstance(exc, ImportError) and ("pillow" in text.lower() or getattr(exc, "name", None) == "PIL"):
        if not _pillow_warned:
            _pillow_warned = True
            log.error("Pillow ist nicht installiert – Bilder in PDF-Dateien (Scans) können nicht gelesen "
                      "werden. Abhilfe: pip install -r requirements.txt")
        return "Pillow nicht installiert"
    name = type(exc).__name__
    if name == "DependencyError":  # pypdf: an external program is missing
        if "jbig2" in text.lower():
            return "JBIG2-Bild, Programm jbig2dec nicht installiert"
        return f"fehlende Komponente ({text})"[:160]
    if name == "UnidentifiedImageError":  # Pillow
        return "Bilddaten beschädigt oder Format unbekannt"
    return f"{name}: {re.sub(r' at 0x[0-9a-fA-F]+', '', text)}"[:160]


# -- the images of a page: found in its content, measured, decoded ---------------------------------------
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
MAX_FORM_DEPTH = 6
_GREY_MODES = ("1", "L", "LA", "I", "I;16", "F")
_OVERSIZED = 1 << 62  # pixel count of an image whose header already says it is too large to decode
# Filters whose decoded data is an image file stating its own size (Pillow reads it from the header):
# DCT = JPEG, JPX = JPEG 2000, CCITT = TIFF (pypdf adds the header), JBIG2 = PNG (from jbig2dec). LZW and
# ASCII85 data is taken as TIFF/PNG by pypdf when it is one, else as raw samples of the declared size.
_FILE_FORMATS = {"/DCTDecode": ("JPEG",), "/JPXDecode": ("JPEG2000",), "/CCITTFaxDecode": ("TIFF",),
                 "/JBIG2Decode": ("PNG", "PPM"), "/LZWDecode": ("TIFF", "PNG"), "/ASCII85Decode": ("TIFF", "PNG")}
_MAYBE_RAW = {"/LZWDecode", "/ASCII85Decode"}
# Abbreviations in inline image dictionaries (PDF 32000-1, tables 92 and 93)
_INLINE_KEYS = {"/BPC": "/BitsPerComponent", "/CS": "/ColorSpace", "/D": "/Decode", "/DP": "/DecodeParms",
                "/F": "/Filter", "/H": "/Height", "/W": "/Width", "/I": "/Interpolate", "/IM": "/ImageMask"}
_INLINE_NAMES = {"/G": "/DeviceGray", "/RGB": "/DeviceRGB", "/CMYK": "/DeviceCMYK", "/I": "/Indexed",
                 "/AHx": "/ASCIIHexDecode", "/A85": "/ASCII85Decode", "/LZW": "/LZWDecode", "/Fl": "/FlateDecode",
                 "/RL": "/RunLengthDecode", "/CCF": "/CCITTFaxDecode", "/DCT": "/DCTDecode"}
_STANDARD_NAMES = set(_INLINE_NAMES.values())


class _PageLimit(Exception):
    """A bound of the content walk (MAX_PAGE_OPERATIONS/FORMS/PLACEMENTS) was reached."""


@dataclass(eq=False)
class _PageImage:
    """One different image of a page: one PDF object (inline images: one piece of image data), however
    often, under whatever names and in whatever forms it is drawn."""
    ident: tuple
    stream: object  # the image stream (an inline image is turned into one)
    declared: tuple[int, int] | None  # /Width × /Height as the image dictionary states them
    # matrices it is drawn at, in drawing order (a dict as ordered set); None: positions unknown
    placements: dict | None = field(default_factory=dict)
    size: tuple[int, int] | None = None  # pixel size it decodes to (set by _measure)
    pixels: int | None = None  # counted against the limits: the larger of declared and real size; None: unknown


@dataclass
class _Drawing:
    """The different images a page draws, in order of their first drawing."""
    images: dict = field(default_factory=dict)  # ident -> _PageImage
    placements: int = 0
    operations: int = 0
    forms: int = 0
    content_bytes: int = 0  # decoded content parsed
    truncated: bool = False  # a page bound stopped the walk: the page may draw more than was found
    unplaced: bool = False  # positions could not be followed (odd matrix values): images are read unplaced

    def add(self, ident: tuple, make, ctm: tuple[float, ...]) -> None:
        image = self.images.get(ident)
        if image is not None and ctm in image.placements:  # the same image at the same place: once
            return
        if self.placements >= MAX_PAGE_PLACEMENTS:
            raise _PageLimit
        if image is None:
            image = self.images[ident] = make()
        image.placements[ctm] = None
        self.placements += 1


def _mul(m, n) -> tuple[float, ...]:
    """PDF matrix product m × n ([a b c d e f])."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _matrix(values) -> tuple[float, ...] | None:
    """Six finite numbers as a matrix, None for anything else."""
    try:
        m = tuple(float(v) for v in values)
    except (TypeError, ValueError):
        return None
    return m if len(m) == 6 and all(math.isfinite(v) for v in m) else None


def _resolved(obj):
    return obj.get_object() if obj is not None and hasattr(obj, "get_object") else obj


def _declared_size(stream) -> tuple[int, int] | None:
    """Width × height as an image dictionary declares them (nothing decoded), None if unknown."""
    try:
        w, h = _resolved(stream.get("/Width")), _resolved(stream.get("/Height"))
        return (max(0, int(w)), max(0, int(h))) if w is not None and h is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _ident(obj) -> tuple:
    """Identity of a PDF object: its object number, or the Python object for a direct one."""
    ref = getattr(obj, "indirect_reference", None)
    if ref is not None:
        return ("obj", ref.idnum, ref.generation)
    return ("id", id(obj))


def _xobject_image(xobj) -> _PageImage:
    return _PageImage(_ident(xobj), xobj, _declared_size(xobj))


def _inline_image(operands, resources) -> tuple[tuple, object]:
    """Identity and maker of an inline image (BI … ID … EI) – the same data drawn again is the same image."""
    from pypdf.generic import ArrayObject, NameObject, StreamObject

    settings = operands.get("settings") or {}
    data = bytes(operands.get("data") or b"")

    def name(key: str, value):
        if not isinstance(value, NameObject):
            return value
        if value in _INLINE_NAMES or value in _STANDARD_NAMES:
            return NameObject(_INLINE_NAMES.get(value, value))
        if key == "/ColorSpace":  # a named colour space of the resources
            spaces = _resolved(resources.get("/ColorSpace")) if resources is not None else None
            found = spaces.get(value) if isinstance(spaces, dict) else None
            return found if found is not None else value
        return value

    entries = {}
    for k, v in settings.items():
        key = _INLINE_KEYS.get(k, k)
        if key in ("/L", "/Length"):
            continue
        entries[NameObject(key)] = ArrayObject(name(key, x) for x in v) if isinstance(v, list) else name(key, v)
    digest = hashlib.blake2b(data, digest_size=16)
    digest.update(repr(sorted((str(k), repr(v)) for k, v in entries.items())).encode())

    def make() -> _PageImage:
        stream = StreamObject.initialize_from_dictionary({"__streamdata__": data, **entries})
        return _PageImage(("inline", digest.hexdigest()), stream, _declared_size(stream))
    return ("inline", digest.hexdigest()), make


def _drawn_images(page, doc: _DocState | None = None) -> _Drawing:
    """The images a page actually draws, with the matrices they are drawn at. Follows q/Q/cm, form
    XObjects (each parsed once) and inline images. Resources listed but never drawn (some PDF tools share
    one resource dictionary between all pages) are not included. Stops – marking the drawing truncated –
    at MAX_PAGE_OPERATIONS operations, MAX_PAGE_FORMS form drawings, MAX_PAGE_PLACEMENTS positions or
    when the content to parse would exceed MAX_PAGE_CONTENT_BYTES (or what is left of the document's
    MAX_DOC_CONTENT_BYTES, charged before parsing). Raises if the content cannot be parsed."""
    from pypdf.generic import ContentStream, StreamObject

    doc = doc if doc is not None else _DocState()
    drawing = _Drawing()
    parsed: dict[tuple, list] = {}  # form -> its operations, parsed once however often it is drawn

    def operations_of(content) -> list:
        size = len(content.get_data())  # decoded, not parsed yet (pypdf parses on .operations)
        if drawing.content_bytes + size > MAX_PAGE_CONTENT_BYTES or doc.content_bytes + size > MAX_DOC_CONTENT_BYTES:
            raise _PageLimit
        drawing.content_bytes += size
        doc.content_bytes += size
        return content.operations

    def walk(operations, resources, ctm, depth: int, active: frozenset) -> None:
        stack: list[tuple[float, ...]] = []
        xobjects = _resolved(resources.get("/XObject")) if resources is not None else None
        if not isinstance(xobjects, dict):
            xobjects = None
        for operands, op in operations:
            drawing.operations += 1
            if drawing.operations > MAX_PAGE_OPERATIONS:
                raise _PageLimit
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                ctm = stack.pop() if stack else ctm
            elif op == b"cm":
                m = _matrix(operands)
                if m is None:
                    drawing.unplaced = True
                else:
                    ctm = _mul(m, ctm)
            elif op == b"INLINE IMAGE":
                ident, make = _inline_image(operands, resources)
                drawing.add(ident, make, ctm)
            elif op == b"Do" and operands and xobjects is not None:
                xobj = _resolved(xobjects.get(str(operands[0])))
                if not isinstance(xobj, StreamObject):
                    continue
                subtype = xobj.get("/Subtype")
                if subtype == "/Image":
                    drawing.add(_ident(xobj), lambda: _xobject_image(xobj), ctm)
                elif subtype == "/Form" and depth < MAX_FORM_DEPTH:
                    ident = _ident(xobj)
                    if ident in active:  # a form drawing itself
                        continue
                    drawing.forms += 1
                    if drawing.forms > MAX_PAGE_FORMS:
                        raise _PageLimit
                    inner_ops = parsed.get(ident)
                    if inner_ops is None:
                        inner_ops = parsed[ident] = operations_of(ContentStream(xobj, page.pdf))
                    matrix = _matrix(_resolved(xobj.get("/Matrix")) or _IDENTITY)
                    if matrix is None:
                        drawing.unplaced, matrix = True, _IDENTITY
                    inner = _resolved(xobj.get("/Resources"))
                    walk(inner_ops, inner if inner is not None else resources, _mul(matrix, ctm), depth + 1,
                         active | {ident})

    content = page.get_contents()
    if content is not None:
        try:
            walk(operations_of(content), _resolved(page.get("/Resources")), _IDENTITY, 0, frozenset())
        except _PageLimit:
            drawing.truncated = True
    if drawing.unplaced:
        for image in drawing.images.values():
            image.placements = None
    return drawing


def _xobjects_of(resources):
    xobjects = _resolved(resources.get("/XObject")) if isinstance(resources, dict) else None
    return xobjects if isinstance(xobjects, dict) else None


def _listed_images(xobjects) -> _Drawing:
    """The images in a page's XObject resources and those of its forms, unplaced – for a page whose
    content cannot be followed or draws none of them itself (e.g. through a pattern). At most
    MAX_PAGE_PLACEMENTS resource entries and MAX_PAGE_FORMS forms are looked at."""
    from pypdf.generic import StreamObject

    drawing = _Drawing(unplaced=True)
    seen: set[tuple] = set()

    def visit(xobjects, depth: int) -> None:
        if xobjects is None:
            return
        for key in list(xobjects.keys()):
            drawing.operations += 1
            if drawing.operations > MAX_PAGE_PLACEMENTS:
                raise _PageLimit
            xobj = _resolved(xobjects.get(key))
            if not isinstance(xobj, StreamObject):
                continue
            ident = _ident(xobj)
            if ident in seen:
                continue
            seen.add(ident)
            subtype = xobj.get("/Subtype")
            if subtype == "/Image":
                image = drawing.images[ident] = _xobject_image(xobj)
                image.placements = None
            elif subtype == "/Form" and depth < MAX_FORM_DEPTH:
                drawing.forms += 1
                if drawing.forms > MAX_PAGE_FORMS:
                    raise _PageLimit
                visit(_xobjects_of(_resolved(xobj.get("/Resources"))), depth + 1)

    try:
        visit(xobjects, 0)
    except _PageLimit:
        drawing.truncated = True
    return drawing


def _page_images(page, name: str, number: int, doc: _DocState) -> _Drawing:
    """The images of a page as drawn; if the content cannot be followed or draws no image, the images
    listed in its resources (unplaced). Raises if not even the resources can be read."""
    try:
        drawing = _drawn_images(page, doc)
    except Exception as exc:  # noqa: BLE001 – unusual content stream: the listed images, unplaced
        log.debug("%s Seite %d: Bildpositionen nicht bestimmbar (%s)", name, number, exc)
        drawing = None
    if drawing is not None and drawing.images:
        return drawing
    xobjects = _xobjects_of(_resolved(page.get("/Resources")))
    cached = doc.listed.get(id(xobjects))  # pages often share their XObject resources
    if cached is None or cached[0] is not xobjects:
        cached = doc.listed[id(xobjects)] = (xobjects, _listed_images(xobjects))
    listed = cached[1]
    if drawing is not None and drawing.truncated and not listed.truncated:
        listed = _Drawing(listed.images, truncated=True, unplaced=True)
    return listed


def _last_filter(stream) -> str | None:
    f = _resolved(stream.get("/Filter"))
    if isinstance(f, list):
        f = _resolved(f[-1]) if f else None
    return str(f) if isinstance(f, str) else None


def _forget_decoded(stream) -> None:
    """pypdf keeps the decoded data of a stream as long as the reader lives; image data is needed once,
    so it is released right away (else a document with many large images would hold all of them)."""
    for s in (stream, _resolved(stream.get("/SMask"))):
        if getattr(s, "decoded_self", None) is not None:
            s.decoded_self = None


def _real_size(stream) -> tuple[int, int] | None:
    """The pixel size the image data itself states (JPEG, JPEG 2000, CCITT, JBIG2), read from its header
    without decoding the pixels; None for raw samples, which are decoded at the declared size. Raises if
    the data is no readable image."""
    last = _last_filter(stream)
    formats = _FILE_FORMATS.get(last)
    if formats is None:
        return None
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(stream.get_data()), formats=formats) as img:  # lazy: header only
            return img.size
    except UnidentifiedImageError:
        if last in _MAYBE_RAW:
            return None
        raise
    finally:
        _forget_decoded(stream)


@dataclass
class _DocState:
    """What the scan pages of one document share: each image (PDF object) is measured, decoded and read
    once per document, and the whole document is bounded (MAX_DOC_*)."""
    vision_calls: int = 0
    decoded_pixels: int = 0
    sizes: dict = field(default_factory=dict)  # image ident -> (size, pixels, problem)
    failed: dict = field(default_factory=dict)  # image ident -> why it could not be decoded
    transcripts: dict = field(default_factory=dict)  # what was sent (_Job.key) -> the model's answer
    listed: dict = field(default_factory=dict)  # id(XObject resources) -> (them, their images)
    content_bytes: int = 0  # decoded content parsed for all pages
    warned: bool = False  # the document bounds were reported

    def charge(self, image: _PageImage) -> None:
        self.decoded_pixels += _decode_charge(image)

    def can_decode(self, *images: _PageImage) -> bool:
        return self.decoded_pixels + sum(_decode_charge(image) for image in images) <= MAX_DOC_PIXELS


def _decode_charge(image: _PageImage) -> int:
    return max(image.pixels or 0, _MIN_DECODE_CHARGE)


_DOC_LIMIT = object()  # _measure: the document's bounds leave no room to measure the image


def _measure(image: _PageImage, doc: _DocState):
    """Sets the image's size and the pixels it counts with; returns why it cannot be decoded, if so (or
    _DOC_LIMIT when the document bounds leave no room to look). Once per image and document."""
    known = doc.sizes.get(image.ident)
    if known is None:
        if _last_filter(image.stream) == "/JBIG2Decode":  # its size is known only after jbig2dec ran
            if not doc.can_decode(image):
                return _DOC_LIMIT
            doc.charge(image)
        known = doc.sizes[image.ident] = _probe(image)
    image.size, image.pixels, problem = known
    return problem


def _probe(image: _PageImage) -> tuple:
    from PIL import Image

    declared = image.declared
    try:
        real = _real_size(image.stream)
    except Image.DecompressionBombError:  # Pillow's own bound (about 179 MP) is above MAX_PAGE_PIXELS
        return declared, _OVERSIZED, None
    except Exception as exc:  # noqa: BLE001 – broken data, missing Pillow/jbig2dec: like a failed decoding
        return None, None, _decode_problem(exc)
    size = real or declared
    if size is None:
        return None, None, None  # unknown: never counted as nothing
    pixels = max(w * h for w, h in (declared or (0, 0), real or (0, 0)))
    return size, pixels, None


@dataclass
class _Decoded:
    image: object  # Pillow image
    data: bytes | None = None  # the image encoded as pypdf hands it out (sent as it is if JPEG/PNG)
    mime: str | None = None


class _Decoder:
    """Decodes page images: ``decoder[image, target]`` with ``target`` the (width, height) the image is
    needed at, or None for its full size. A mapping like pypdf's ``page.images``, which it replaces:
    that one decodes every inline image of a page at the first access and lists all images again on
    each one."""

    def __getitem__(self, request) -> _Decoded:
        image, target = request
        return _decode(image, target)


def _decode(image: _PageImage, target: tuple[int, int] | None) -> _Decoded:
    stream = image.stream
    try:
        size = image.size
        if target is not None and size is not None and size[0] * size[1] > target[0] * target[1]:
            reduced = _decode_reduced(stream, target)
            if reduced is not None:
                return _Decoded(reduced)
        from pypdf.generic._image_xobject import _xobj_to_image  # what page.images uses for one image

        ext, data, img = _xobj_to_image(stream)
        return _Decoded(img, data, _SENDABLE.get((ext or "").lower()))
    finally:
        _forget_decoded(stream)


def _plain_colours(stream) -> bool:
    """No soft mask, no /Decode remapping and a colour space whose samples Pillow reads as they are."""
    if "/SMask" in stream or "/Decode" in stream:
        return False
    space = _resolved(stream.get("/ColorSpace"))
    if isinstance(space, str):
        return space in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK")
    if isinstance(space, list):
        return bool(space) and _resolved(space[0]) in ("/ICCBased", "/CalGray", "/CalRGB")
    return space is None or type(space).__name__ == "NullObject"


def _decode_reduced(stream, target: tuple[int, int]):
    """A large JPEG or JPEG 2000 image decoded directly at reduced scale – libjpeg scales by 1/2, 1/4 or
    1/8 while decoding, OpenJPEG drops resolution levels – to the smallest such size that still covers
    ``target``: an A0 plan never takes its full size in memory. None for images that need pypdf's colour
    handling (masks, /Decode, indexed or special colour spaces); those are decoded in full."""
    from PIL import Image, ImageChops

    last = _last_filter(stream)
    if last not in ("/DCTDecode", "/JPXDecode") or not _plain_colours(stream):
        return None
    img = Image.open(io.BytesIO(stream.get_data()), formats=("JPEG",) if last == "/DCTDecode" else ("JPEG2000",))
    if last == "/DCTDecode":
        if img.mode not in ("L", "RGB", "CMYK"):
            return None
        img.draft(img.mode, target)
    else:
        if img.mode not in ("L", "RGB"):
            return None
        width, height = img.size
        levels = 0
        while levels < 5 and width >> (levels + 1) >= target[0] and height >> (levels + 1) >= target[1]:
            levels += 1
        img.reduce = levels
    img.load()
    if img.size[0] * img.size[1] > target[0] * target[1]:  # scaled the rest of the way right away
        img = img.resize(target, Image.Resampling.LANCZOS)
    if img.mode == "CMYK":  # read as pypdf does: CMYK JPEG data is stored inverted (Adobe)
        img = ImageChops.invert(img)
    return img


def _image_payload(image: _Decoded) -> tuple[bytes, str]:
    """Bytes and MIME type of a decoded page image in a format multimodal models accept (JPEG/PNG).

    JPEG and PNG within the size limit are sent as pypdf hands them out; other formats (JPEG 2000, TIFF
    from CCITT fax or LZW, CMYK), images decoded at reduced scale and very large images are encoded with
    Pillow."""
    pil = image.image
    width, height = pil.size
    fits = width * height <= MAX_IMAGE_PIXELS
    if fits and image.data and image.mime == "image/png":
        return image.data, image.mime
    if fits and image.data and image.mime == "image/jpeg" and pil.mode in ("L", "RGB"):
        return image.data, image.mime
    return _encode(pil)


def _encode(img) -> tuple[bytes, str]:
    """A Pillow image as JPEG (colour) or PNG (bilevel/grey), scaled down to MAX_IMAGE_PIXELS."""
    from PIL import Image  # installed with requirements.txt; pypdf needs it for images anyway

    width, height = img.size
    if img.mode not in ("1", "L", "RGB"):
        img = img.convert("RGB")  # CMYK, palette, alpha channel, 16 bit
    if width * height > MAX_IMAGE_PIXELS:
        if img.mode == "1":
            img = img.convert("L")  # scaling bilevel scans keeps text readable in grey
        f = (MAX_IMAGE_PIXELS / (width * height)) ** 0.5
        img = img.resize((max(1, int(width * f)), max(1, int(height * f))), Image.Resampling.LANCZOS)
    out = io.BytesIO()
    if img.mode == "RGB":  # photos and colour plans: JPEG keeps the request small
        img.save(out, "JPEG", quality=90)
        return out.getvalue(), "image/jpeg"
    img.save(out, "PNG", optimize=True)  # bilevel/grey scans: lossless and compact
    return out.getvalue(), "image/png"


def _fit(size: tuple[int, int], limit: int) -> tuple[int, int] | None:
    """The size an image of ``size`` is scaled down to for ``limit`` pixels, None if it fits."""
    width, height = size
    if width * height <= limit:
        return None
    f = (limit / (width * height)) ** 0.5
    return max(1, int(width * f)), max(1, int(height * f))


# -- putting a page together ----------------------------------------------------------------------------
@dataclass
class _Tile:
    """One drawing of an image on the page image."""
    image: _PageImage
    turned: bool  # image columns run down the page
    flip_x: bool
    flip_y: bool
    target: tuple[int, int]  # size on the page image (pixels)
    position: tuple[int, int]

    @property
    def shape(self) -> tuple:
        return self.turned, self.flip_x, self.flip_y, self.target

    @property
    def needed(self) -> tuple[int, int]:
        """Pixels of the image (in its own orientation) this drawing needs."""
        return (self.target[1], self.target[0]) if self.turned else self.target


def _corners(ctm) -> dict[str, tuple[float, float]]:
    """Where the image's top-left, top-right and bottom-left corners land on the page (points)."""
    a, b, c, d, e, f = ctm
    return {"tl": (c + e, d + f), "tr": (a + c + e, b + d + f), "bl": (e, f)}


def _layout(chosen: list[_PageImage]) -> tuple[tuple[int, int], list[_Tile]] | None:
    """Where the images of a page go on one page image, from their placements and real pixel sizes –
    before anything is decoded. None when they cannot be put together: unknown positions, skewed or empty
    placements, or more pasting and rescaling than _COMPOSE_WORK_FACTOR page images (heavy overlap)."""
    if any(image.placements is None or not image.placements for image in chosen):
        return None
    boxes = []
    scale = 0.0
    for image in chosen:
        for ctm in image.placements:
            k = _corners(ctm)
            right = (k["tr"][0] - k["tl"][0], k["tr"][1] - k["tl"][1])
            down = (k["bl"][0] - k["tl"][0], k["bl"][1] - k["tl"][1])
            straight = (abs(right[1]) < 1e-6 * max(1.0, abs(right[0]))
                        and abs(down[0]) < 1e-6 * max(1.0, abs(down[1])))
            turned = (abs(right[0]) < 1e-6 * max(1.0, abs(right[1]))
                      and abs(down[1]) < 1e-6 * max(1.0, abs(down[0])))
            if not (straight or turned):
                return None  # skewed or rotated by an odd angle
            width_pt = abs(right[0]) + abs(down[0])
            height_pt = abs(right[1]) + abs(down[1])
            if width_pt < 1e-3 or height_pt < 1e-3:
                return None
            xs = [p[0] for p in k.values()] + [k["tr"][0] + down[0]]
            ys = [p[1] for p in k.values()] + [k["tr"][1] + down[1]]
            boxes.append((image, right, down, turned, min(xs), max(ys), width_pt, height_pt))
            pixels_wide = image.size[1] if turned else image.size[0]
            scale = max(scale, pixels_wide / width_pt)
    left = min(b[4] for b in boxes)
    top = max(b[5] for b in boxes)
    right_edge = max(b[4] + b[6] for b in boxes)
    bottom = min(b[5] - b[7] for b in boxes)
    w_pt, h_pt = right_edge - left, top - bottom
    scale = min(scale, (MAX_IMAGE_PIXELS / (w_pt * h_pt)) ** 0.5)
    size = (max(1, round(w_pt * scale)), max(1, round(h_pt * scale)))
    tiles = []
    for image, right, down, turned, x0, y0, wp, hp in boxes:
        if turned:
            right, down = down, right
        tiles.append(_Tile(image, turned, right[0] < 0, down[1] > 0,
                           (max(1, round(wp * scale)), max(1, round(hp * scale))),
                           (round((x0 - left) * scale), round((top - y0) * scale))))
    work = sum(t.target[0] * t.target[1] for t in tiles)  # pasting
    for group in _by_image(tiles).values():  # an image drawn at several sizes is rescaled once more each
        shapes = {t.shape for t in group}
        if len(shapes) > 1:
            need = _needed(group)
            work += (len(shapes) - 1) * need[0] * need[1]
    return (size, tiles) if work <= _COMPOSE_WORK_FACTOR * MAX_IMAGE_PIXELS else None


def _by_image(tiles: list[_Tile]) -> dict[tuple, list[_Tile]]:
    groups: dict[tuple, list[_Tile]] = {}
    for tile in tiles:
        groups.setdefault(tile.image.ident, []).append(tile)
    return groups


def _needed(group: list[_Tile]) -> tuple[int, int]:
    return max(t.needed[0] for t in group), max(t.needed[1] for t in group)


def _compose(layout, decode):
    """Put the images of a page together into one page image (Pillow), or None if none could be decoded.
    The images are decoded one after the other – each at the size its largest drawing needs – pasted at
    all their places and released, so memory holds the page image and one image at a time. An image's
    drawings are pasted together, in the order of its first drawing (exact only where images overlap,
    which strip and tile scans do not). ``decode(image, needed)`` returns a _Decoded or None."""
    from PIL import Image

    size, tiles = layout
    canvas = Image.new("L", size, "white")
    pasted = 0
    for group in _by_image(tiles).values():
        need = _needed(group)
        decoded = decode(group[0].image, need)
        if decoded is None:
            continue
        source = decoded.image
        decoded = None
        if source.mode not in _GREY_MODES and canvas.mode == "L":
            canvas = canvas.convert("RGB")
        shapes = {t.shape for t in group}
        if len(shapes) > 1 and source.size[0] * source.size[1] > need[0] * need[1]:
            source = _scaled(source, need)  # rescaled once; each size is then made from the smaller image
        ready: dict[tuple, tuple] = {}
        for tile in group:
            if tile.shape not in ready:
                ready[tile.shape] = _prepared(source, *tile.shape, canvas.mode)
            piece, mask = ready[tile.shape]
            canvas.paste(piece, tile.position, mask)
        pasted += 1
    return canvas if pasted else None


def _scaled(img, size: tuple[int, int]):
    from PIL import Image

    if img.mode == "1":
        img = img.convert("L")
    elif img.mode == "P":
        img = img.convert("RGBA")
    return img.resize(size, Image.Resampling.LANCZOS)


def _prepared(img, turned: bool, flip_x: bool, flip_y: bool, target: tuple[int, int], mode: str):
    """An image turned, flipped and scaled for its place on the page, with its transparency mask."""
    from PIL import Image

    if turned:  # image columns run down the page: swap axes first
        img = img.transpose(Image.Transpose.TRANSPOSE)
    if flip_x:
        img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if flip_y:  # PDF y grows upwards: the image's rows run up the page
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    mask = None
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        mask = img.getchannel("A").resize(target, Image.Resampling.BILINEAR)
    if img.mode == "1":
        img = img.convert("L")  # scaling bilevel scans keeps text readable in grey
    return img.convert(mode).resize(target, Image.Resampling.LANCZOS), mask


# -- reading the pages -------------------------------------------------------------------------------------
@dataclass
class _Job:
    """One image for the model: a page image (possibly put together) – or its answer, already known."""
    key: tuple  # what is sent: ("image", ident) or ("page", images with their placements)
    text: str | None = None  # the answer read for the same key on an earlier page
    data: bytes | None = None
    mime: str | None = None
    reusable: bool = True  # complete (no image of it failed): its answer may stand for the same key


@dataclass
class _PageOutcome:
    jobs: list[_Job] = field(default_factory=list)
    undecodable: int = 0  # images that could not be decoded
    left_out: int = 0  # images beyond MAX_IMAGES_PER_PAGE that could not be put together
    over_limit: int = 0  # images beyond MAX_PAGE_IMAGES/MAX_PAGE_PIXELS
    doc_limited: int = 0  # images left out because the document's bounds are used up
    truncated: bool = False  # the content walk hit a page bound


def _read_scanned_pages(empty_pages, llm, name: str) -> _ScanResult:
    """Let the multimodal model read the images of pages without a text layer.

    Up to MAX_IMAGES_PER_PAGE images of a page are sent one by one; a page with more (strip or tile
    scans) is put together into one page image at the images' positions. Pages without any image
    (blank pages) count as nothing to read. A page none of whose images could be read counts as
    undecodable or failed – the document is retried later; only a page whose images the model read
    without finding text counts as "no text". A page of which only some images were read counts as
    read *partly*: its text is kept and the document stays incomplete (and is retried when the cause
    may go away). An image (or page image) already read on an earlier page is not sent again: its
    answer is reused."""
    result = _ScanResult()
    doc = _DocState()
    decoder = _Decoder()
    for i, page in empty_pages:
        try:
            drawn = _page_images(page, name, i, doc)
        except Exception as exc:  # noqa: BLE001 – broken resources: the page cannot be read now
            log.warning("%s Seite %d: Bilder nicht lesbar (%s)", name, i, exc)
            result.decode_error = result.decode_error or _decode_problem(exc)
            result.undecodable += 1
            continue
        if drawn.truncated:
            log.warning("%s Seite %d: Seiteninhalt nicht vollständig durchsucht (%s)", name, i, _page_bounds())
        if not drawn.images:
            if drawn.truncated:  # no image found before the bound – the page is not known to be blank
                result.too_complex += 1
            continue
        out = _page_payloads(decoder, drawn, name, i, result, doc)
        if out.doc_limited and not doc.warned:
            doc.warned = True
            log.warning("%s: ab Seite %d nicht mehr alle Bilder ausgewertet (%s)", name, i, _doc_bounds())
        texts, failed = [], 0
        for job in out.jobs:
            text = job.text
            if text is None:
                doc.vision_calls += 1
                try:
                    text = llm.describe_image(job.data, job.mime, VISION_PROMPT).text or ""
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s Seite %d: Bildauswertung fehlgeschlagen (%s)", name, i, exc)
                    failed += 1
                    continue
                if job.reusable:
                    doc.transcripts[job.key] = text
            if text.strip():
                texts.append(text)
        if texts:
            result.pages.append(Page(i, "\n".join(texts)))
            if out.undecodable or failed or out.left_out or out.over_limit or out.doc_limited or out.truncated:
                result.partial += 1
                result.partial_undecodable += out.undecodable
                result.partial_failed += failed
                result.partial_left_out += out.left_out
                result.partial_over_limit += out.over_limit
                result.partial_doc_limit += out.doc_limited
                result.partial_truncated += out.truncated
                log.warning("%s Seite %d nur teilweise gelesen (%d nicht lesbar, %d fehlgeschlagen, %d nicht "
                            "ausgewertet%s)", name, i, out.undecodable, failed,
                            out.left_out + out.over_limit + out.doc_limited,
                            ", Inhalt nicht vollständig durchsucht" if out.truncated else "")
        elif out.undecodable:
            result.undecodable += 1
        elif failed:
            result.model_failed += 1
        elif out.jobs:
            result.no_text += 1
        elif out.over_limit:
            result.too_large += 1
        elif out.doc_limited:
            result.doc_limit += 1
        elif out.truncated:
            result.too_complex += 1
        else:
            result.no_text += 1
    return result


def _page_payloads(images, drawn: _Drawing, name: str, number: int, result: _ScanResult,
                   doc: _DocState | None = None) -> _PageOutcome:
    """The images of one page for the model (as _Job), with the numbers of images that could not be
    decoded, that were left out (more than MAX_IMAGES_PER_PAGE, not put together), that were over the
    page limits and over the document's bounds. ``images`` decodes (see _Decoder).

    Each different image is decoded and sent once, however often and under whatever names the page
    draws it – and once per document: an image read on an earlier page is not decoded again. Images
    beyond MAX_PAGE_IMAGES/MAX_PAGE_PIXELS (real sizes, measured from the headers) are left out
    undecoded. More than MAX_IMAGES_PER_PAGE different images are put together into one page image;
    if that is not possible, the largest ones are sent."""
    doc = doc if doc is not None else _DocState()
    out = _PageOutcome(truncated=drawn.truncated)
    chosen, broken = _choose(drawn, doc, out, name, number, result)
    new_jobs = 0

    def decode(image: _PageImage, target):
        """Decodes one image (charged to the document), or counts why not and returns None."""
        if image.ident in doc.failed:  # failed on an earlier page of this document
            out.undecodable += 1
            return None
        doc.charge(image)
        try:
            return images[image, target]
        except Exception as exc:  # noqa: BLE001 – missing Pillow/jbig2dec, unsupported or broken image
            log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, number, exc)
            problem = doc.failed[image.ident] = _decode_problem(exc)
            result.decode_error = result.decode_error or problem
            out.undecodable += 1
            return None

    if len(chosen) + broken > MAX_IMAGES_PER_PAGE and chosen:
        layout = _layout(chosen)
        if layout is not None:
            key = ("page", tuple((image.ident, tuple(image.placements)) for image in chosen))
            if key in doc.transcripts:
                out.jobs.append(_Job(key, text=doc.transcripts[key]))
                return out
            if doc.vision_calls >= MAX_DOC_VISION_CALLS or not doc.can_decode(*chosen):
                out.doc_limited += len(chosen)
                return out
            undecodable = out.undecodable
            try:
                combined = _compose(layout, decode)
                if combined is not None:
                    data, mime = _encode(combined)
                    out.jobs.append(_Job(key, data=data, mime=mime, reusable=out.undecodable == undecodable))
                return out
            except Exception as exc:  # noqa: BLE001 – e.g. out of memory: fall back to the largest images
                log.warning("%s Seite %d: Bilder nicht zusammensetzbar (%s)", name, number, exc)
                out.undecodable = undecodable  # counted again below
        chosen = sorted(chosen, key=lambda image: -image.pixels)  # stable: equal sizes keep drawing order
    for k, image in enumerate(chosen):
        if len(out.jobs) >= MAX_IMAGES_PER_PAGE:
            out.left_out += len(chosen) - k
            break
        key = ("image", image.ident)
        if key in doc.transcripts:
            out.jobs.append(_Job(key, text=doc.transcripts[key]))
            continue
        if doc.vision_calls + new_jobs >= MAX_DOC_VISION_CALLS or not doc.can_decode(image):
            out.doc_limited += 1
            continue
        decoded = decode(image, _fit(image.size, MAX_IMAGE_PIXELS))
        if decoded is None:
            continue
        try:
            data, mime = _image_payload(decoded)
        except Exception as exc:  # noqa: BLE001 – unsupported format, conversion failed
            log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, number, exc)
            problem = doc.failed[image.ident] = _decode_problem(exc)
            result.decode_error = result.decode_error or problem
            out.undecodable += 1
            continue
        finally:
            decoded = None
        out.jobs.append(_Job(key, data=data, mime=mime))
        new_jobs += 1
    return out


def _choose(drawn: _Drawing, doc: _DocState, out: _PageOutcome, name: str, number: int, result: _ScanResult):
    """The images of a page within MAX_PAGE_IMAGES and MAX_PAGE_PIXELS, in drawing order – measured (real
    size from the header) but not decoded – and the number of images whose data is no readable image."""
    chosen, pixels, broken = [], 0, 0
    for image in drawn.images.values():
        if len(chosen) >= MAX_PAGE_IMAGES:
            out.over_limit += 1
            continue
        declared = image.declared[0] * image.declared[1] if image.declared else 0
        if pixels + declared > MAX_PAGE_PIXELS:  # over the limit whatever the data says
            out.over_limit += 1
            continue
        first = image.ident not in doc.sizes
        problem = _measure(image, doc)
        if problem is _DOC_LIMIT:
            out.doc_limited += 1
        elif problem is not None:
            if first:  # reported once per document
                log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, number, problem)
            result.decode_error = result.decode_error or problem
            out.undecodable += 1
            broken += 1
        elif image.pixels is None or pixels + image.pixels > MAX_PAGE_PIXELS:
            out.over_limit += 1
        else:
            chosen.append(image)
            pixels += image.pixels
    if out.over_limit:
        log.warning("%s Seite %d: %d Bild(er) nicht ausgewertet (mehr als %d Bilder bzw. %d Megapixel je Seite)",
                    name, number, out.over_limit, MAX_PAGE_IMAGES, MAX_PAGE_PIXELS // 1_000_000)
    return chosen, broken


def _docx(data: bytes) -> Extracted:
    import docx  # python-docx

    document = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    body = document.element.body
    # paragraphs and tables in document order
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = docx.text.paragraph.Paragraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            style = (para.style.name if para.style is not None else "") or ""
            if style.lower().startswith(("heading", "überschrift", "title", "titel")):
                lines.append(f"\n# {text}")
            else:
                lines.append(text)
        elif tag == "tbl":
            table = docx.table.Table(child, document)
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                lines.append(" | ".join(c for c in cells if c))
    return Extracted([Page(None, "\n".join(lines).strip())])


def _eml(data: bytes) -> Extracted:
    msg = email.message_from_bytes(data, policy=policy.default)
    head = f"Betreff: {msg.get('Subject', '')}\nVon: {msg.get('From', '')}\nDatum: {msg.get('Date', '')}"
    body = msg.get_body(("plain", "html"))
    text = ""
    if body is not None:
        text = mime_text(body)
        if body.get_content_type() == "text/html":
            text = html_to_text(text)
    return Extracted([Page(None, f"{head}\n\n{text}".strip())])


def extract(path: Path | str, data: bytes | None = None, *, llm=None) -> Extracted:
    """Extract text from a file. ``data`` may be given (mail attachments); the file is only read."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT:
        return Extracted([], status="skipped", message=f"Format {ext or '(ohne Endung)'} nicht unterstützt")
    try:
        if data is None:
            data = path.read_bytes()
        if ext in TEXT_EXT:
            result = Extracted([Page(None, decode_text(data))])
        elif ext == ".pdf":
            result = _pdf(data, llm, path.name)
        elif ext == ".docx":
            result = _docx(data)
        elif ext == ".eml":
            result = _eml(data)
        else:  # images: plans, photos, scans
            if llm is None or not getattr(llm, "vision", False):
                return Extracted([], status="skipped", incomplete=True,
                                 message="Bild – Bildauswertung ausgeschaltet (LLM_VISION=0)")
            text = llm.describe_image(data, IMAGE_EXT[ext], VISION_PROMPT).text
            result = Extracted([Page(1, text)], method="vision", incomplete=not (text or "").strip())
    except Exception as exc:  # noqa: BLE001 – one broken file must not stop indexing
        log.warning("Extraktion fehlgeschlagen: %s (%s)", path.name, exc)
        return Extracted([], status="error", message=f"{type(exc).__name__}: {exc}"[:300],
                         incomplete=True, retry=True)
    if result.status == "ok" and not result.text.strip():
        result.status, result.message = "empty", "kein Text gefunden"
    return result
