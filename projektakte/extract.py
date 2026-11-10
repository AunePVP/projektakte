"""Text extraction: classic extraction first, the multimodal model for scans and plans (only with
LLM_VISION=1), Tesseract (if installed) for scanned pages without a text layer.

A PDF page counts as a *scan* when it has no text layer and draws an image, or when it has a text layer
(an OCR layer) and draws a large image covering much of the page (see _is_scan); digital pages never go
to the model. In an indexing pass the model is not called while the file is read (``defer_vision``):
the scan pages get their text layer or Tesseract's text at once and are listed in
``Extracted.vision_pages``, read later by the background phase "Bildauswertung" (vision.py) with the
page image *and* that text. Called directly (scripts, tests), the model reads them right away. Without
vision, such files are marked incomplete, so they are read once vision is switched on (see
documents.reprocess_due)."""
from __future__ import annotations

import codecs
import email
import hashlib
import io
import logging
import math
import re
import shutil
import struct
import subprocess
import zlib
import zipfile
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from email import policy
from html.parser import HTMLParser
from pathlib import Path
from xml.parsers import expat

log = logging.getLogger("projektakte.extract")

TEXT_EXT = {".txt", ".text", ".md", ".markdown", ".csv", ".log"}
IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".tif": "image/tiff", ".tiff": "image/tiff", ".webp": "image/webp"}
# OpenDocument (LibreOffice, OpenOffice) text, spreadsheet and presentation files with their templates, and
# their predecessors in the OpenOffice.org 1.x/StarOffice XML format – the same ZIP package with content.xml
# (see _odf). The binary StarOffice 5 formats (.sdw, .sdc, .sdd) are something else and not read.
ODF_EXT = {".odt", ".ott", ".ods", ".ots", ".odp", ".otp", ".sxw", ".stw", ".sxc", ".stc", ".sxi", ".sti"}
SUPPORTED_EXT = TEXT_EXT | {".pdf", ".docx", ".eml"} | ODF_EXT | set(IMAGE_EXT)
# Formats whose structure is parsed (PDF objects and images, ZIP/XML, MIME): during an indexing pass with
# EXTRACT_ISOLATION they are read in the extraction worker process with operating-system limits (see
# extract_worker). Text files are only decoded, and images go to the vision model as they are.
STRUCTURED_EXT = {".pdf", ".docx", ".eml"} | ODF_EXT
# The address-space limit of the process in MB, if it has one (set in the extraction worker): for messages,
# and a result cut short by it names it (Extracted.limit), so a higher limit reads the file again.
MEMORY_LIMIT_MB: int | None = None
# Called before each page of a PDF is read, if set. The extraction worker ends itself there when the app
# that started it has gone (see extract_worker._serve).
PAGE_CHECK: Callable[[], None] | None = None
# Called after each page read by Tesseract and each page image prepared, if set: the extraction worker
# tells the app that the work goes on (its wall-clock backstop starts again, see extract_worker._serve).
PROGRESS: Callable[[], None] | None = None
_VISION_RULES = (
    "Füge handschriftliche Notizen, Häkchen, Kreuze und Markierungen als „[handschriftlich: …]“ und Stempel als "
    "„[Stempel: …]“ genau an der Stelle ein, zu der sie gehören (dieselbe Zeile, Tabellenzeile oder Position). "
    "Beschreibe bei Plänen kurz Inhalt, Beschriftungen und Brandschutz-Symbole. Unleserliches als „[unleserlich]“. "
    "Erfinde nichts. Antworte nur mit dem Text der Seite."
)
VISION_PROMPT = (
    "Transkribiere den gesamten lesbaren Text dieses gescannten Dokuments bzw. Plans auf Deutsch, vollständig "
    "und getreu. " + _VISION_RULES
)
# Text already known for a page (OCR text layer or Tesseract) handed to the model with the page image, at
# most this many characters (a dense A4 page has about 4.000).
VISION_TEXT_CHARS = 12_000


def vision_prompt(known: str = "") -> str:
    """The prompt for one page image: with the text already recognised on the page (text layer or Tesseract)
    the model is asked to give the whole page faithfully, correct recognition errors from the image and put
    handwriting and stamps where they belong; without it, to transcribe the page (VISION_PROMPT)."""
    known = (known or "").strip()
    if not known:
        return VISION_PROMPT
    if len(known) > VISION_TEXT_CHARS:
        known = known[:VISION_TEXT_CHARS] + " …"
    return ("Dies ist eine gescannte Seite. Darunter steht der Text, den eine Texterkennung auf ihr gefunden hat – er "
            "kann Fehler enthalten, und Handschrift, Häkchen und Stempel fehlen darin. Gib den vollständigen Text der "
            "Seite getreu wieder: Übernimm den erkannten Text, berichtige Erkennungsfehler anhand des Bildes und ergänze, "
            "was fehlt. " + _VISION_RULES + "\n\nErkannter Text:\n<<<\n" + known + "\n>>>")


# -- Tesseract (optional): the text of scanned pages without a text layer, before the model reads them ------
OCR_COMMAND = "tesseract"
OCR_LANGUAGE = "deu"
OCR_TIMEOUT = 60.0  # seconds per page image (a dense A4 page at 300 dpi takes a few seconds, a large plan more)
# Page images are scaled down to this before Tesseract reads them: A4 at 300 dpi has 8.7 MP; larger
# (plans) take Tesseract minutes and give little text.
OCR_MAX_PIXELS = 9_000_000
_ocr_known: dict[str, bool] = {}
_OCR_WORD = re.compile(r"[^\W\d_]{3,}")


def ocr_available() -> bool:
    """Is Tesseract installed with the German language data? Checked once per process (installing it means
    a new container or restart). Without it scanned pages are read as before: by the model alone."""
    if "ok" not in _ocr_known:
        _ocr_known["ok"] = _probe_ocr()
    return _ocr_known["ok"]


def _probe_ocr() -> bool:
    exe = shutil.which(OCR_COMMAND)
    if exe is None:
        log.info("Texterkennung (Tesseract) nicht installiert – Scans ohne Textebene liest nur das Bildmodell")
        return False
    try:
        done = subprocess.run([exe, "--list-langs"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Texterkennung (Tesseract) nicht nutzbar (%s)", exc)
        return False
    if OCR_LANGUAGE not in (done.stdout + done.stderr).decode("utf-8", "replace").split():
        log.warning("Texterkennung (Tesseract) ohne deutsche Sprachdaten (Paket tesseract-ocr-%s) – nicht genutzt",
                    OCR_LANGUAGE)
        return False
    return True


def ocr_image(data: bytes) -> str:
    """The text Tesseract recognises in an image (JPEG, PNG, TIFF), "" if none or if it fails (logged). Runs
    as a program of its own with a time limit; in the extraction worker it inherits the worker's memory
    limit. Text with fewer than three words is dropped (photos give stray letters)."""
    exe = shutil.which(OCR_COMMAND)
    if exe is None:
        return ""
    data = _for_ocr(data)
    try:
        done = subprocess.run([exe, "stdin", "stdout", "-l", OCR_LANGUAGE, "--psm", "3"], input=data,
                              capture_output=True, timeout=OCR_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        log.warning("Texterkennung abgebrochen (über %d s)", OCR_TIMEOUT)
        return ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Texterkennung fehlgeschlagen (%s)", exc)
        return ""
    finally:
        _progress()
    if done.returncode != 0:
        lines = done.stderr.decode("utf-8", "replace").strip().splitlines()
        log.warning("Texterkennung fehlgeschlagen (%s)", (lines[-1] if lines else f"Exitcode {done.returncode}")[:200])
        return ""
    return clean_ocr(done.stdout.decode("utf-8", "replace"))


def _for_ocr(data: bytes) -> bytes:
    """The image at most OCR_MAX_PIXELS (a page image of the extraction is a JPEG or PNG of up to
    MAX_IMAGE_PIXELS); anything that cannot be measured goes to Tesseract as it is."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            size = _fit(img.size, OCR_MAX_PIXELS)
            if size is None or size == img.size or img.format not in ("JPEG", "PNG"):
                return data
            small = img.convert("L").resize(size)
        out = io.BytesIO()
        small.save(out, "PNG")
        return out.getvalue()
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 – Tesseract reads it itself or says why not
        return data


def clean_ocr(text: str) -> str:
    """Tesseract's output tidied: no form feeds, at most one empty line in a row, nothing if it holds fewer
    than three words of three letters or more."""
    lines = [line.rstrip() for line in text.replace("\f", "\n").splitlines()]
    out: list[str] = []
    for line in lines:
        if line.strip() or (out and out[-1]):
            out.append(line if line.strip() else "")
    text = "\n".join(out).strip()
    return text if len(_OCR_WORD.findall(text)) >= 3 else ""


def _progress() -> None:
    if PROGRESS is not None:
        PROGRESS()


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
# 6: soft masks (/SMask) are measured, counted and applied by the extraction itself (onto white), never
#    in chains; JPEG 2000 and JPEGs with /Decode or a soft mask are decoded at reduced scale; pages that
#    can draw no image (vector drawings, CAD plans with outlined text) count as blank instead of "Inhalt
#    zu umfangreich"; images that are too large to decode (memory, pypdf's data limits) or have no size
#    are told apart and not retried – incomplete documents of version 5 are read again.
# 7: scanned pages with a text layer (an OCR layer under a large page image) go to the model as well, with
#    image and text (handwriting and stamps are only in the image); pages without a text layer are read
#    by Tesseract first (if installed); in an indexing pass the model reads scan pages later, in the
#    phase "Bildauswertung" – incomplete documents of version 6 are read again, and complete PDFs of
#    earlier versions read without the model are checked for such pages there (vision.py).
# A newly read format (OpenDocument) is no reason to bump: a bump reads incomplete documents again, and no
# file of a format not read before is stored as incomplete (project files of such a format were not stored at
# all, mail attachments as complete with "Format … nicht unterstützt") – it would only cost a pass over every
# incomplete document (scans without vision, failed model calls) for nothing.
EXTRACT_VERSION = 7
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
# cost, JBIG2 even starts a program): bounds documents made of thousands of tiny images. Decoded data
# beyond what the image's size needs (raw samples that inflate far beyond /Width × /Height, which pypdf
# decompresses all the same, up to 75 MB) is charged on top, one byte as one pixel: the charge follows the
# work, not the declaration.
_MIN_DECODE_CHARGE = 250_000
_BYTES_PER_PIXEL = 4  # decoded data an image may plausibly need per pixel (8-bit CMYK) before it is excess
# A JPEG 2000 image that cannot be decoded at reduced scale (indexed or special colour spaces, alpha in the
# data, 16 bit) goes through pypdf, which decodes it in full – OpenJPEG holds four bytes per sample – and
# encodes and decodes it once more: about 36 bytes per pixel. It counts this many times its pixels.
_JPX_FULL_DECODE_WEIGHT = 8
# Putting a page together may paste and rescale at most this multiple of MAX_IMAGE_PIXELS (strips and
# tiles cover the page about once; heavily overlapping placements are no strip or tile scan).
_COMPOSE_WORK_FACTOR = 8
_SENDABLE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
# A page with a text layer counts as a scan (an OCR layer under the scanned image) when the images it draws
# have at least this many pixels together and cover at least this share of the page. A logo, a stamp image
# or a photo in a report is smaller; a page whose image positions cannot be followed counts by the pixels.
SCAN_MIN_PIXELS = 1_000_000
SCAN_MIN_COVERAGE = 0.5
# Status of a re-check (extract(..., recheck=True)) that found no scanned page: nothing to store anew.
UNCHANGED = "unchanged"


def extraction_profile(llm, ocr: bool | None = None) -> str:
    """What the extraction can read with this model: without vision the LLM plays no part; Tesseract
    (``ocr``, default: whether it is installed) adds "+ocr".

    Stored per document; incomplete documents are processed again when the profile changes
    (e.g. LLM_VISION switched on, another multimodal model, Tesseract installed)."""
    ocr = ocr_available() if ocr is None else ocr
    tail = "+ocr" if ocr else ""
    if llm is not None and getattr(llm, "vision", False):
        return f"v{EXTRACT_VERSION}:vision:{getattr(llm, 'model', '') or '?'}{tail}"
    return f"v{EXTRACT_VERSION}:text{tail}"


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
    # the operating-system limit of the extraction worker that cut the result short, as "memory=<MB>" or
    # "cpu=<seconds>" (see extract_worker.Limits.lifts): the file is read again once that limit is raised
    # (documents.reprocess_due), although the file and the profile stay the same
    limit: str | None = None
    # pages the model is to read later (extract(..., defer_vision=True)): scan pages, held in ``pages`` with
    # their text layer or Tesseract's text for now; ``incomplete`` then speaks of the rest of the file only
    vision_pages: list[int] = field(default_factory=list)

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


# Tokens pypdf parses in one content stream (its default, 2 million, cuts off the labels of large CAD
# plans); measured: 10 million take about 28 s and 1.2 GB – within the extraction worker's limits.
PDF_STREAM_TOKENS = 8_000_000


def _pdf(data: bytes, llm, name: str, **kw) -> Extracted:
    from pypdf import apply_configuration

    with apply_configuration(stream_parsing_work_maximum_count=PDF_STREAM_TOKENS):
        return _pdf_pages(data, llm, name, **kw)


def _open_pdf(data: bytes):
    """A pypdf reader of the file, None if it is encrypted with a password."""
    from pypdf import PdfReader
    from pypdf.errors import FileNotDecryptedError

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        # many PDFs are only protected against editing or printing: they open with an empty password
        try:
            opened = reader.decrypt("")
        except FileNotDecryptedError:
            opened = 0
        if not opened:
            return None
    return reader


def _pdf_pages(data: bytes, llm, name: str, *, vision: bool | None = None, defer: bool = False,
               ocr: bool = False, recheck: bool = False) -> Extracted:
    """The pages of a PDF. Pages with a text layer keep it; scan pages (see _is_scan) are read by the model
    – right away, or with ``defer`` later (``Extracted.vision_pages``) – and pages without a text layer by
    Tesseract first if ``ocr``. ``recheck``: a file read before scan pages with a text layer were told
    apart – if no page can be one, the result is UNCHANGED (nothing needs to be stored again)."""
    reader = _open_pdf(data)
    if reader is None:
        return Extracted([], status="skipped", message=PDF_ENCRYPTED_TEXT)
    vision = (llm is not None and getattr(llm, "vision", False)) if vision is None else vision
    doc = _DocState()
    if recheck and not any(_may_be_scan(page, doc) for page in reader.pages):
        return Extracted([], status=UNCHANGED)
    pages = []
    empty_pages = []
    layer_scans = []  # pages with a text layer under a large image (an OCR layer): (number, page)
    texts: dict[tuple, str] = {}  # pages drawing the same content with the same resources: one extraction
    for i, page in enumerate(reader.pages, start=1):
        _check_page()
        key = _text_key(page)
        try:
            text = texts[key] if key in texts else page.extract_text() or ""
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 – broken pages must not stop indexing
            log.warning("%s Seite %d: Text nicht lesbar (%s)", name, i, exc)
            text = ""
        if key is not None:
            texts[key] = text
        if len(text.strip()) < 20:
            empty_pages.append((i, page))
        else:
            pages.append(Page(i, text))
            if _is_scan(page, doc, name, i):
                layer_scans.append((i, page))
    if recheck and not layer_scans and not empty_pages:
        return Extracted([], status=UNCHANGED)
    if not empty_pages and not layer_scans:
        return Extracted(pages)
    if not vision:
        return _without_vision(pages, empty_pages, layer_scans, name, doc, ocr)
    if defer:
        return _vision_later(pages, empty_pages, layer_scans, name, doc, ocr)
    bases = {p.number: p.text for p in pages} if layer_scans else {}
    layer = {i for i, _page in layer_scans}
    items = sorted(empty_pages + layer_scans, key=lambda item: item[0])
    scan = _read_scanned_pages(items, llm, name, ocr=ocr, bases={i: bases[i] for i in layer}, doc=doc)
    read = {p.number for p in scan.pages}
    pages = sorted([p for p in pages if p.number not in read] + scan.pages, key=lambda p: p.number or 0)
    method = "vision" if scan.vision_read else "text"
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


_VISION_OFF = "Bildauswertung ausgeschaltet (LLM_VISION=0)"


def _without_vision(pages, empty_pages, layer_scans, name: str, doc: _DocState, ocr: bool) -> Extracted:
    """LLM_VISION=0: pages without a text layer are read by Tesseract if ``ocr``; the file stays incomplete
    (a profile with vision reads its scan pages), also for scan pages with a text layer."""
    notes = []
    read = 0
    if empty_pages and ocr:
        scan = _read_scanned_pages(empty_pages, None, name, ocr=True, doc=doc, compose=True)
        read = len(scan.pages)
        pages = sorted(pages + scan.pages, key=lambda p: p.number or 0)
        if read:
            notes.append(f"{read} Seite(n) ohne Textebene per Texterkennung gelesen")
        if not scan.with_images:  # blank pages and vector drawings only
            empty_pages = []
        elif len(scan.with_images) > read:
            notes.append(f"{len(scan.with_images) - read} Seite(n) ohne Textebene: Texterkennung ergab keinen Text")
    elif empty_pages:
        notes.append(f"{len(empty_pages)} Seite(n) ohne Textebene")
    if layer_scans:
        notes.append(f"{len(layer_scans)} gescannte Seite(n) mit Textebene")
    if not empty_pages and not layer_scans:
        return Extracted(pages) if pages else Extracted([], status="empty", message="kein Text gefunden")
    if pages:
        return Extracted(pages, incomplete=True, message=f"{'; '.join(notes)} – {_VISION_OFF}")
    if ocr:
        return Extracted([], status="skipped", incomplete=True,
                         message=f"Scan ohne Textebene, Texterkennung ergab keinen Text – {_VISION_OFF}")
    return Extracted([], status="skipped", incomplete=True, message=f"Scan ohne Textebene – {_VISION_OFF}")


def _vision_later(pages, empty_pages, layer_scans, name: str, doc: _DocState, ocr: bool) -> Extracted:
    """Stage 1 of an indexing pass with vision: the file is stored at once with the text it has – text
    layers, and Tesseract's text for pages without one (if ``ocr``) – and its scan pages are listed for the
    model (Extracted.vision_pages, read by the phase "Bildauswertung"). No model call here."""
    scan = _read_scanned_pages(empty_pages, None, name, ocr=ocr, doc=doc, compose=True) if empty_pages else None
    later = sorted({i for i, _page in layer_scans} | set(scan.with_images if scan else ()))
    pages = sorted(pages + (scan.pages if scan else []), key=lambda p: p.number or 0)
    notes = []
    incomplete = False
    if len(later) > MAX_DOC_VISION_CALLS:
        notes.append(f"{len(later) - MAX_DOC_VISION_CALLS} Seite(n) nicht ausgewertet ({_doc_bounds()})")
        later, incomplete = later[:MAX_DOC_VISION_CALLS], True
    retry = False
    if scan is not None and (scan.too_complex or scan.undecodable):  # pages whose content could not be read
        notes.append(f"{scan.too_complex + scan.undecodable} Seite(n) ohne Textebene nicht gelesen ({scan.reason()})")
        incomplete, retry = True, bool(scan.undecodable)
    message = "; ".join(notes) or None
    if not later:
        if pages:
            return Extracted(pages, message=message, incomplete=incomplete, retry=retry)
        if incomplete:
            return Extracted([], status="error" if retry else "skipped", message=f"Scan ohne Textebene – {message}",
                             incomplete=True, retry=retry)
        return Extracted([], status="empty", message="kein Text gefunden")
    if pages:
        return Extracted(pages, message=message, incomplete=incomplete, retry=retry, vision_pages=later)
    return Extracted([], status="skipped", message=message, incomplete=incomplete, retry=retry, vision_pages=later)


def _may_be_scan(page, doc: _DocState) -> bool:
    """Could the page be a scan with a text layer? Only if the images in its resources (and those of its
    forms) declare SCAN_MIN_PIXELS together – nothing parsed or decoded (a cheap first look, see _is_scan)."""
    try:
        listed = _listed(page, doc)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 – unreadable resources: the walk decides
        return True
    if listed.truncated:
        return True
    pixels = 0
    for image in listed.images.values():
        if image.declared:
            pixels += image.declared[0] * image.declared[1]
    return pixels >= SCAN_MIN_PIXELS


def _is_scan(page, doc: _DocState, name: str, number: int) -> bool:
    """Is a page with a text layer a scan – an OCR layer under the scanned page image? The images it draws
    have SCAN_MIN_PIXELS together and cover SCAN_MIN_COVERAGE of the page (strips and tiles count together;
    by the pixels alone if their positions cannot be followed). Digital pages with a logo or a photo are not:
    they never go to the model."""
    if not _may_be_scan(page, doc):
        return False
    key = _text_key(page)
    drawn = doc.drawings.get(key) if key is not None else None
    try:
        if drawn is None:
            drawn = _page_images(page, name, number, doc)
            if key is not None:
                doc.drawings[key] = drawn
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 – broken resources: kept as a digital page
        log.debug("%s Seite %d: Bilder nicht bestimmbar (%s)", name, number, exc)
        return False
    pixels, covered = 0, 0.0
    unplaced = False
    for image in drawn.images.values():
        if image.declared:
            pixels += image.declared[0] * image.declared[1]
        if image.placements is None:
            unplaced = True
            continue
        for ctm in image.placements:
            covered += abs(ctm[0] * ctm[3] - ctm[1] * ctm[2])
    if pixels < SCAN_MIN_PIXELS:
        return False
    if unplaced:
        return True
    try:
        box = page.mediabox
        area = abs(float(box.width) * float(box.height))
    except Exception:  # noqa: BLE001 – no usable page size: the pixels decide
        return True
    return area <= 0 or covered >= SCAN_MIN_COVERAGE * area


def _text_key(page) -> tuple | None:
    """What a page's text depends on – its content streams (by PDF object), resources (by PDF object, or
    the entries of a small direct dictionary) and rotation; None if that cannot be told. A file repeating
    one content on thousands of pages has its text extracted once."""
    contents = page.get("/Contents")
    parts = []
    for obj in contents if isinstance(contents, list) else [contents]:
        if getattr(obj, "idnum", None) is None:
            return None
        parts.append((obj.idnum, obj.generation))
    resources = page.get("/Resources")
    if getattr(resources, "idnum", None) is not None:
        parts.append((resources.idnum, resources.generation))
    else:  # written into each page: equal entries (references to the same objects) are the same resources
        text = repr(resources)
        if len(text) > 10_000:
            return None
        parts.append(text)
    return tuple(parts), str(page.get("/Rotate", 0))


def _n(number: int) -> str:
    """A number with German thousands separators."""
    return f"{number:,}".replace(",", ".")


def _memory_bound() -> str:
    if MEMORY_LIMIT_MB:
        return f"Speichergrenze von {_n(MEMORY_LIMIT_MB)} MB erreicht"
    return "nicht genug Speicher"


def too_big_to_read() -> Extracted:
    """The result for a file whose extraction ran out of memory: deterministic within the worker's memory
    limit, so it is not retried (another extraction version, or a higher limit, reads it again)."""
    return Extracted([], status="skipped", message=f"Datei zu groß zum Lesen ({_memory_bound()})",
                     incomplete=True)


class _Shortage:
    """Whether the extraction of one file ran out of memory somewhere and went on without that part."""
    met = False


_SHORTAGE: ContextVar[_Shortage | None] = ContextVar("extract_shortage", default=None)


def _out_of_memory(exc: BaseException) -> bool:
    """Is ``exc`` a MemoryError? If so, it is remembered for the file being extracted: within a memory
    limit, a higher limit may read what was left out (see Extracted.limit)."""
    if not isinstance(exc, MemoryError):
        return False
    shortage = _SHORTAGE.get()
    if shortage is not None:
        shortage.met = True
    return True


def _too_big(exc: BaseException) -> bool:
    """Decoding failed because the image is too large: memory, pypdf's limits on decoded data, Pillow's
    decompression bomb check – deterministic, not worth a retry."""
    return _out_of_memory(exc) or type(exc).__name__ in ("LimitReachedError", "DecompressionBombError")


def _check_page() -> None:
    """Before a page is read: PAGE_CHECK, if set (the extraction worker ends there if the app has gone)."""
    if PAGE_CHECK is not None:
        PAGE_CHECK()


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
    too_big: int = 0  # its images too large to decode (memory, pypdf's data limits)
    unsized: int = 0  # its images state no size (raw samples without /Width or /Height)
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
    partial_too_big: int = 0
    partial_unsized: int = 0
    partial_doc_limit: int = 0
    partial_truncated: int = 0
    with_images: list = field(default_factory=list)  # numbers of the pages that draw an image
    vision_read: int = 0  # pages the model gave text for

    @property
    def unread(self) -> int:
        return (self.no_text + self.undecodable + self.model_failed + self.too_large + self.too_big
                + self.unsized + self.too_complex + self.doc_limit)

    @property
    def retry(self) -> bool:
        """A failure that may go away (model back, missing component installed): try again later."""
        return bool(self.undecodable or self.model_failed or self.partial_undecodable or self.partial_failed)

    def partial_note(self) -> str:
        """German note on the pages that were read only partly."""
        return f"{self.partial} Seite(n) nur teilweise gelesen ({self.partial_reasons()})"

    def partial_reasons(self) -> str:
        """Why pages were read only partly (German)."""
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
        if self.partial_too_big:
            parts.append(f"{self.partial_too_big} Bild(er) zu groß zum Lesen ({_IMAGE_BOUND})")
        if self.partial_unsized:
            parts.append(f"{self.partial_unsized} Bild(er) ohne Größenangabe nicht ausgewertet")
        if self.partial_doc_limit:
            parts.append(f"{self.partial_doc_limit} Bild(er) nicht ausgewertet ({_doc_bounds()})")
        if self.partial_truncated:
            parts.append(f"Inhalt von {self.partial_truncated} Seite(n) nicht vollständig durchsucht "
                         f"({_page_bounds()})")
        return "; ".join(parts)

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
        if self.too_big:
            reasons.append((self.too_big, f"Bilder zu groß zum Lesen ({_IMAGE_BOUND})"))
        if self.unsized:
            reasons.append((self.unsized, "Bilder ohne Größenangabe (Breite/Höhe fehlen)"))
        if self.too_complex:
            reasons.append((self.too_complex, f"Seiteninhalt zu umfangreich ({_page_bounds()})"))
        if self.doc_limit:
            reasons.append((self.doc_limit, f"Dokument zu umfangreich ({_doc_bounds()})"))
        if len(reasons) > 1:  # say which reason applies to how many pages
            return "; ".join(f"{n}× {text}" for n, text in reasons)
        return "; ".join(text for _n_pages, text in reasons)


_IMAGE_BOUND = "Speicher- oder Datengrenze überschritten"
_pillow_warned = False


def _decode_problem(exc: Exception) -> str:
    """Short German description of why a PDF image could not be decoded."""
    global _pillow_warned
    if isinstance(exc, MemoryError):
        return _memory_bound()
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
    # counted against the limits: the larger of declared and real size (weighted for a JPEG 2000 image decoded
    # in full, see _JPX_FULL_DECODE_WEIGHT); None: unknown
    pixels: int | None = None
    mask: _Mask | None = None  # its soft mask (/SMask), shared with the other images using it (set by _measure)


@dataclass(eq=False)
class _Mask:
    """A soft mask (/SMask): an image of its own, measured once per document and decoded at the sizes
    the images using it need (kept for reuse: many images may share one mask). Its own /SMask is ignored,
    as the PDF specification says – masks are never followed in chains."""
    ident: tuple
    stream: object
    size: tuple[int, int]
    pixels: int
    decoded: dict = field(default_factory=dict)  # size needed -> mask image (mode L), see _MASK_CACHE_PIXELS
    broken: bool = False  # its data could not be decoded: the images using it are read without it


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
    form_streams: list = field(default_factory=list)  # forms visited (_listed_images only)

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
                drawing.form_streams.append(xobj)
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
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 – unusual content stream: the listed images, unplaced
        log.debug("%s Seite %d: Bildpositionen nicht bestimmbar (%s)", name, number, exc)
        drawing = None
    if drawing is not None and drawing.images:
        return drawing
    listed = _listed(page, doc)
    if drawing is not None and drawing.truncated and not listed.truncated:
        listed = _Drawing(listed.images, truncated=True, unplaced=True)
    return listed


def _listed(page, doc: _DocState) -> _Drawing:
    """The images listed in the page's XObject resources (see _listed_images), once per resources."""
    xobjects = _xobjects_of(_resolved(page.get("/Resources")))
    cached = doc.listed.get(id(xobjects))  # pages often share their XObject resources
    if cached is None or cached[0] is not xobjects:
        cached = doc.listed[id(xobjects)] = (xobjects, _listed_images(xobjects))
    return cached[1]


# The inline image operator as a token of its own (PDF delimiters or white space around it).
_INLINE_IMAGE = re.compile(rb"(?<![^\s\x00\[\]()<>{}/%])BI(?=[\s\x00/])")
# Decoded content searched for inline images per document (about a second): beyond, pages are walked.
MAX_DOC_SEARCH_BYTES = 1_000_000_000


def _can_draw_images(page, doc: _DocState) -> bool:
    """Could the page draw an image for the model? Not if its resources (and those of their forms) list no
    image and neither its content nor the forms' contain an inline image: a vector drawing – a CAD plan
    with its text as outlines – or a blank page, for the model nothing to read however large its content.
    Checked by searching the decoded bytes, without parsing them, so such pages use none of the content
    bounds. When in doubt: yes."""
    listed = _listed(page, doc)
    if listed.images or listed.truncated:
        return True
    try:
        contents = page.get_contents()
        for stream in ([contents] if contents is not None else []) + listed.form_streams:
            data = stream.get_data()
            doc.searched_bytes += len(data)
            if stream is not contents:
                _forget_decoded(stream)  # forms are PDF objects that live with the reader
            if doc.searched_bytes > MAX_DOC_SEARCH_BYTES or _INLINE_IMAGE.search(data):
                return True
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 – unreadable content: the walk decides as before
        return True
    return False


def _last_filter(stream) -> str | None:
    f = _resolved(stream.get("/Filter"))
    if isinstance(f, list):
        f = _resolved(f[-1]) if f else None
    return str(f) if isinstance(f, str) else None


def _forget_decoded(stream) -> None:
    """pypdf keeps the decoded data of a stream as long as the reader lives; image data is needed once,
    so it is released right away (else a document with many large images would hold all of them)."""
    if getattr(stream, "decoded_self", None) is not None:
        stream.decoded_self = None


def _decoded_length(stream) -> int:
    """Length of the stream's decoded data, if it was decoded (pypdf keeps it until _forget_decoded)."""
    cached = getattr(stream, "decoded_self", None)
    if cached is not None:
        return len(cached.get_data())
    return 0 if _resolved(stream.get("/Filter")) else len(getattr(stream, "_data", b"") or b"")


def _without_mask(stream):
    """The image stream without its /SMask (the same data): pypdf would decode the mask in full, follow
    chains of masks and encode masked JPEGs once more as JPEG 2000 – masks are measured and applied by
    the extraction itself (_Mask)."""
    if "/SMask" not in stream:
        return stream
    from pypdf.generic import StreamObject

    entries = {k: v for k, v in stream.items() if k != "/SMask"}
    return StreamObject.initialize_from_dictionary({"__streamdata__": stream._data, **entries})


def _header(stream) -> tuple[tuple[int, int], str] | None:
    """The pixel size and Pillow mode the image data itself states (JPEG, JPEG 2000, CCITT, JBIG2), read
    from its header without decoding the pixels; None for raw samples, which are decoded at the declared
    size. Raises if the data is no readable image."""
    last = _last_filter(stream)
    formats = _FILE_FORMATS.get(last)
    if formats is None:
        return None
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(stream.get_data()), formats=formats) as img:  # lazy: header only
            return img.size, img.mode
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
    decoded_pixels: int = 0  # work charged against MAX_DOC_PIXELS (see _MIN_DECODE_CHARGE)
    sizes: dict = field(default_factory=dict)  # image ident -> (size, pixels, problem, mask)
    masks: dict = field(default_factory=dict)  # mask ident -> _Mask, or None if it is unusable
    failed: dict = field(default_factory=dict)  # image ident -> why it could not be decoded
    too_big: set = field(default_factory=set)  # image idents too large to decode (memory, data limits)
    transcripts: dict = field(default_factory=dict)  # what was sent (_Job.key) -> the model's answer
    listed: dict = field(default_factory=dict)  # id(XObject resources) -> (them, their images)
    drawings: dict = field(default_factory=dict)  # _text_key of a page -> what it draws
    content_bytes: int = 0  # decoded content parsed for all pages
    searched_bytes: int = 0  # decoded content searched for inline images (_can_draw_images)
    warned: bool = False  # the document bounds were reported

    def charge(self, image: _PageImage) -> None:
        self.decoded_pixels += _decode_charge(image)

    def can_decode(self, *images: _PageImage) -> bool:
        """Is there room for decoding these images? A soft mask shared by several of them counts once
        (it is decoded once at the size they need)."""
        masks = {image.mask.ident: image.mask.pixels for image in images if image.mask is not None}
        need = sum(max(image.pixels or 0, _MIN_DECODE_CHARGE) for image in images) + sum(masks.values())
        return self.decoded_pixels + need <= MAX_DOC_PIXELS


def _decode_charge(image: _PageImage) -> int:
    """The work one decoding of the image is expected to take (before it is known): its pixels, at least
    _MIN_DECODE_CHARGE, and those of its soft mask."""
    return max(image.pixels or 0, _MIN_DECODE_CHARGE) + (image.mask.pixels if image.mask is not None else 0)


_DOC_LIMIT = object()  # _measure: the document's bounds leave no room to measure the image
_TOO_BIG = object()  # _probe: the image's data is too large to decode (memory, pypdf's data limits)


def _measure(image: _PageImage, doc: _DocState):
    """Sets the image's size, the pixels it counts with and its soft mask; returns why it cannot be
    decoded, if so (_TOO_BIG, or _DOC_LIMIT when the document bounds leave no room to look). Once per image
    and document."""
    known = doc.sizes.get(image.ident)
    if known is None:
        if _last_filter(image.stream) == "/JBIG2Decode":  # its size is known only after jbig2dec ran
            if not doc.can_decode(image):
                return _DOC_LIMIT
            doc.charge(image)
        size, pixels, problem = _probe(image.stream, image.declared)
        mask = _mask_of(image, doc) if problem is None and size is not None else None
        if mask is _TOO_BIG:
            problem, mask = _TOO_BIG, None
        known = doc.sizes[image.ident] = (size, pixels, problem, mask)
    image.size, image.pixels, problem, image.mask = known
    return problem


def _probe(stream, declared) -> tuple:
    """(size, pixels counted, problem) of an image stream, from its header – nothing decoded."""
    from PIL import Image

    try:
        header = _header(stream)
    except Image.DecompressionBombError:  # Pillow's own bound (about 179 MP) is above MAX_PAGE_PIXELS
        return declared, _OVERSIZED, None
    except Exception as exc:  # noqa: BLE001 – broken data, missing Pillow/jbig2dec: like a failed decoding
        if _too_big(exc):  # the data itself is beyond pypdf's limits or the memory: never decodable
            return declared, None, _TOO_BIG
        return None, None, _decode_problem(exc)
    real = header[0] if header else None
    size = real or declared
    if size is None:
        return None, None, None  # unknown: never counted as nothing
    pixels = max(w * h for w, h in (declared or (0, 0), real or (0, 0)))
    if (header and _last_filter(stream) == "/JPXDecode"
            and not (_plain_colours(stream) and header[1] in _REDUCIBLE_MODES["/JPXDecode"])):
        pixels *= _JPX_FULL_DECODE_WEIGHT  # decoded in full by pypdf (see _decode_reduced)
    return size, pixels, None


def _mask_of(image: _PageImage, doc: _DocState):
    """The image's soft mask (measured once per document), None if it has none or an unusable one (the
    image is then read without it), _TOO_BIG if its data is too large to decode."""
    from pypdf.generic import StreamObject

    stream = _resolved(image.stream.get("/SMask"))
    if not isinstance(stream, StreamObject):
        return None
    ident = _ident(stream)
    if ident not in doc.masks:
        size, pixels, problem = _probe(stream, _declared_size(stream))
        if problem is _TOO_BIG:
            doc.masks[ident] = _TOO_BIG
        elif problem is not None or size is None or not size[0] or not size[1]:
            log.debug("Weiche Maske nicht lesbar, Bild ohne Maske gelesen (%s)", problem or "ohne Größe")
            doc.masks[ident] = None
        else:
            doc.masks[ident] = _Mask(ident, _without_mask(stream), size, pixels)
    return doc.masks[ident]


@dataclass
class _Decoded:
    image: object  # Pillow image (with an alpha channel if a soft mask was applied)
    data: bytes | None = None  # the image encoded as pypdf hands it out (sent as it is if JPEG/PNG)
    mime: str | None = None
    work: int = 0  # charged against MAX_DOC_PIXELS: pixels, excess decoded data and a mask decoded


class _Decoder:
    """Decodes page images: ``decoder[image, target]`` with ``target`` the (width, height) the image is
    needed at, or None for its full size. A mapping like pypdf's ``page.images``, which it replaces:
    that one decodes every inline image of a page at the first access and lists all images again on
    each one."""

    def __getitem__(self, request) -> _Decoded:
        image, target = request
        return _decode(image, target)


def _decode(image: _PageImage, target: tuple[int, int] | None) -> _Decoded:
    """Decode an image (without its soft mask, see _without_mask) – at reduced scale where the format
    allows – and apply its soft mask as alpha channel."""
    decoded = _decode_stream(_without_mask(image.stream), image.size, image.pixels, target)
    mask = image.mask
    if mask is not None and not mask.broken:
        try:
            alpha, work = _mask_image(mask, target or image.size, decoded.image.size)
        except Exception as exc:  # noqa: BLE001 – a broken mask: the image is read without it
            if _too_big(exc):
                raise
            log.debug("Weiche Maske nicht lesbar, Bild ohne Maske gelesen (%s)", exc)
            mask.broken = True
        else:
            decoded = _Decoded(_with_alpha(decoded.image, alpha), work=decoded.work + work)
    return decoded


def _decode_stream(stream, size, pixels, target) -> _Decoded:
    try:
        img = None
        larger = target is not None and size is not None and size[0] * size[1] > target[0] * target[1]
        if larger or (size is not None and _last_filter(stream) == "/JPXDecode"):
            # JPEG 2000 directly also at full size: pypdf would encode it once more and decode it again
            img = _decode_reduced(stream, target if larger else size)
        if img is not None:
            decoded = _Decoded(img)
        else:
            from pypdf.generic._image_xobject import _xobj_to_image  # what page.images uses for one image

            ext, data, img = _xobj_to_image(stream)
            decoded = _Decoded(img, data, _SENDABLE.get((ext or "").lower()))
        # raw samples that inflate far beyond the image's size cost work the declaration does not show
        need = size[0] * size[1] * _BYTES_PER_PIXEL if size else 0
        decoded.work = max(pixels or 0, _MIN_DECODE_CHARGE) + max(0, _decoded_length(stream) - need)
        return decoded
    finally:
        _forget_decoded(stream)


# Most masks are shared by images of the same size: decoded masks are kept per document, together at most
# this many pixels (mode L: one byte each).
_MASK_CACHE_PIXELS = MAX_IMAGE_PIXELS


def _mask_image(mask: _Mask, needed: tuple[int, int], size: tuple[int, int]):
    """The soft mask as an alpha channel (mode L) of ``size``, decoded at the size ``needed`` (reduced
    where the format allows), and the work that took (0 if it was decoded before)."""
    from PIL import Image

    alpha = mask.decoded.get(needed)
    work = 0
    if alpha is None:
        decoded = _decode_stream(mask.stream, mask.size, mask.pixels, _fit(mask.size, needed[0] * needed[1]))
        alpha = decoded.image if decoded.image.mode == "L" else decoded.image.convert("L")
        if alpha.size != needed:
            alpha = alpha.resize(needed, Image.Resampling.BILINEAR)
        work = decoded.work
        kept = sum(w * h for w, h in mask.decoded)
        if kept + needed[0] * needed[1] <= _MASK_CACHE_PIXELS:
            mask.decoded[needed] = alpha
    if alpha.size != size:  # reduced decoding stops at a scale step: the image may be a little larger
        alpha = alpha.resize(size, Image.Resampling.BILINEAR)
    return alpha, work


def _with_alpha(img, alpha):
    """The image with the mask as alpha channel (LA for bilevel and grey, RGBA otherwise)."""
    if img.mode in _GREY_MODES:
        img = img.convert("L")
    elif img.mode != "RGB":
        img = img.convert("RGB")
    img.putalpha(alpha)
    return img


# Pillow modes of JPEG and JPEG 2000 data that are decoded at reduced scale; others go through pypdf.
_REDUCIBLE_MODES = {"/DCTDecode": ("L", "RGB", "CMYK"), "/JPXDecode": ("L", "RGB")}


def _plain_colours(stream) -> bool:
    """A colour space whose samples Pillow reads as they are (a /Decode array is applied afterwards)."""
    space = _resolved(stream.get("/ColorSpace"))
    if isinstance(space, str):
        return space in ("/DeviceGray", "/DeviceRGB", "/DeviceCMYK")
    if isinstance(space, list):
        return bool(space) and _resolved(space[0]) in ("/ICCBased", "/CalGray", "/CalRGB")
    return space is None or type(space).__name__ == "NullObject"


def _decode_reduced(stream, target: tuple[int, int]):
    """A JPEG or JPEG 2000 image decoded directly at reduced scale – libjpeg scales by 1/2, 1/4 or 1/8
    while decoding, OpenJPEG drops resolution levels – to the smallest such size that still covers
    ``target``: an A0 plan never takes its full size in memory. The colours are those of pypdf (its /Decode
    handling, inverted CMYK JPEG data). None for images that need pypdf's colour handling (indexed or
    special colour spaces, alpha or 16 bit in the data); those are decoded in full."""
    from PIL import Image
    from pypdf.generic._image_xobject import _apply_decode

    last = _last_filter(stream)
    if last not in _REDUCIBLE_MODES or not _plain_colours(stream):
        return None
    img = Image.open(io.BytesIO(stream.get_data()), formats=("JPEG",) if last == "/DCTDecode" else ("JPEG2000",))
    if img.mode not in _REDUCIBLE_MODES[last]:
        return None
    if last == "/DCTDecode":
        img.draft(img.mode, target)
    else:
        width, height = img.size
        levels = 0
        while levels < 5 and width >> (levels + 1) >= target[0] and height >> (levels + 1) >= target[1]:
            levels += 1
        img.reduce = levels
    img.load()
    if img.size[0] * img.size[1] > target[0] * target[1]:  # scaled the rest of the way right away
        img = img.resize(target, Image.Resampling.LANCZOS)
    space = _resolved(stream.get("/ColorSpace"))
    if isinstance(space, list) and len(space) == 1:
        space = _resolved(space[0])
    # as pypdf: CMYK JPEG data is stored inverted (Adobe), a /Decode array remaps the samples (pypdf's own
    # inversion flag only matters for grey images in colour spaces that are not plain)
    return _apply_decode(img, stream, last, space, False)


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
    if img.mode in ("P", "PA"):
        img = img.convert("RGBA" if img.mode == "PA" or "transparency" in img.info else "RGB")
    elif img.mode not in ("1", "L", "LA", "RGB", "RGBA"):
        img = img.convert("RGB")  # CMYK, 16 bit
    if width * height > MAX_IMAGE_PIXELS:
        if img.mode == "1":
            img = img.convert("L")  # scaling bilevel scans keeps text readable in grey
        f = (MAX_IMAGE_PIXELS / (width * height)) ** 0.5
        img = img.resize((max(1, int(width * f)), max(1, int(height * f))), Image.Resampling.LANCZOS)
    if img.mode in ("LA", "RGBA"):  # a soft mask: the transparent parts show the white page
        img = _on_white(img)
    out = io.BytesIO()
    if img.mode == "RGB":  # photos and colour plans: JPEG keeps the request small
        img.save(out, "JPEG", quality=90)
        return out.getvalue(), "image/jpeg"
    img.save(out, "PNG", optimize=True)  # bilevel/grey scans: lossless and compact
    return out.getvalue(), "image/png"


def _on_white(img):
    """An image with alpha channel (LA or RGBA) put onto white, as on a white page."""
    from PIL import Image

    mode = img.mode[:-1]
    base = Image.new(mode, img.size, "white")
    base.paste(img.convert(mode), mask=img.getchannel("A"))
    return base


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
    """An image turned, flipped and scaled for its place on the page, with its transparency mask. It is
    scaled first – in its own orientation – and only then turned and converted to the mode of the page
    image: an A0 bilevel plan is never converted to colour at its full size."""
    from PIL import Image

    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    elif img.mode == "PA":
        img = img.convert("RGBA")
    elif img.mode == "1":
        img = img.convert("L")  # scaling bilevel scans keeps text readable in grey
    elif img.mode not in ("L", "LA", "RGB", "RGBA", "CMYK"):  # 16 bit and other modes Pillow cannot scale
        img = img.convert(mode)
    size = (target[1], target[0]) if turned else target  # in the image's own orientation
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    mask = None
    if img.mode in ("LA", "RGBA"):
        mask = img.getchannel("A")
        img = img.convert(img.mode[:-1])
    for flag, how in ((turned, Image.Transpose.TRANSPOSE), (flip_x, Image.Transpose.FLIP_LEFT_RIGHT),
                      (flip_y, Image.Transpose.FLIP_TOP_BOTTOM)):  # PDF y grows upwards: rows may run up
        if flag:
            img = img.transpose(how)
            mask = mask.transpose(how) if mask is not None else None
    return img.convert(mode), mask


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
    too_big: int = 0  # images too large to decode (memory, pypdf's data limits)
    unsized: int = 0  # images without a size (raw samples without /Width or /Height)
    doc_limited: int = 0  # images left out because the document's bounds are used up
    truncated: bool = False  # the content walk hit a page bound

    def missing(self) -> int:
        """Images of the page not read for a reason that is not a failure (no retry)."""
        return self.left_out + self.over_limit + self.too_big + self.unsized + self.doc_limited


def _read_scanned_pages(empty_pages, llm, name: str, *, ocr: bool = False, bases: dict | None = None,
                        doc: _DocState | None = None, compose: bool = False) -> _ScanResult:
    """Let the multimodal model (and Tesseract, if ``ocr``) read the images of scan pages – pages without
    a text layer, and those in ``bases`` (number → their text layer), which is handed to the model with
    the image and stands if the model gives nothing. ``llm`` None: Tesseract alone (``ocr``), or – neither –
    only which pages draw an image (``with_images``), nothing decoded.

    Up to MAX_IMAGES_PER_PAGE images of a page are sent one by one; a page with more (strip or tile
    scans) – with ``compose``, and for a page with a text layer, any page with more than one – is put
    together into one page image at the images' positions. Pages without any image (blank pages, vector
    drawings) count as nothing to read. A page none of whose images could be read counts as undecodable
    or failed – the document is retried later; only a page whose images the model read without finding
    text counts as "no text". A page of which only some images were read counts as read *partly*: its
    text is kept and the document stays incomplete (and is retried when the cause may go away). An image
    (or page image) already read on an earlier page is not sent again: its answer is reused. Without a
    text layer, Tesseract's text goes to the model with the image (and stands if the model fails)."""
    result = _ScanResult()
    doc = doc if doc is not None else _DocState()
    bases = bases or {}
    decoder = _Decoder()
    total = len(empty_pages)
    reading = llm is not None or ocr
    what = "Bildauswertung" if llm is not None else "Texterkennung"
    if reading:
        log.info("%s: %s – %d Seite(n)%s", what, name, total, "" if bases else " ohne Textebene")
    every = 1 if total <= 50 else 10  # (the worker forwards at most 200 log records per file)
    for n, (i, page) in enumerate(empty_pages, start=1):
        _check_page()
        if reading and n > 1 and (n % every == 0 or n == total):
            log.info("%s: %s – Seite %d von %d", what, name, n, total)
        key = _text_key(page)  # pages with the same content and resources draw the same
        drawn = doc.drawings.get(key) if key is not None else None
        base = bases.get(i, "")
        try:
            if drawn is None:
                # nothing the model could read on vector drawings and blank pages, however large the content
                drawn = _page_images(page, name, i, doc) if _can_draw_images(page, doc) else _Drawing()
                if key is not None:
                    doc.drawings[key] = drawn
        except MemoryError:
            raise
        except Exception as exc:  # noqa: BLE001 – broken resources: the page cannot be read now
            log.warning("%s Seite %d: Bilder nicht lesbar (%s)", name, i, exc)
            result.decode_error = result.decode_error or _decode_problem(exc)
            if base:  # the text layer stands; the page is read partly
                result.pages.append(Page(i, base))
                result.partial += 1
                result.partial_undecodable += 1
            else:
                result.undecodable += 1
            continue
        if drawn.truncated:
            log.warning("%s Seite %d: Seiteninhalt nicht vollständig durchsucht (%s)", name, i, _page_bounds())
        if not drawn.images:
            if base:
                result.pages.append(Page(i, base))
            elif drawn.truncated:  # no image found before the bound – the page is not known to be blank
                result.too_complex += 1
            continue
        result.with_images.append(i)
        if not reading:
            continue
        if compose or base:  # one page image: the model sees the whole page next to its text
            out = _page_payloads(decoder, drawn, name, i, result, doc, 1)
        else:
            out = _page_payloads(decoder, drawn, name, i, result, doc)
        if out.doc_limited and not doc.warned:
            doc.warned = True
            log.warning("%s: ab Seite %d nicht mehr alle Bilder ausgewertet (%s)", name, i, _doc_bounds())
        texts, kept, failed, answered = [], [], 0, False
        for job in out.jobs:
            text = job.text
            if text is None:
                known = base or (ocr_image(job.data) if ocr else "")
                if llm is None:
                    text = known
                else:
                    doc.vision_calls += 1
                    try:
                        text = llm.describe_image(job.data, job.mime, vision_prompt(known)).text or ""
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s Seite %d: Bildauswertung fehlgeschlagen (%s)", name, i, exc)
                        failed += 1
                        if known.strip() and not base:
                            kept.append(known)  # Tesseract's text stands for now
                        continue
                    answered = answered or bool(text.strip())
                    if not text.strip() and not base:
                        text = known
                if job.reusable:
                    doc.transcripts[job.key] = text
            if text.strip():
                texts.append(text)
        texts += kept
        if base and not texts:  # the model gave nothing: the text layer stands
            texts = [base]
        result.vision_read += answered
        if texts:
            result.pages.append(Page(i, "\n".join(texts)))
            if out.undecodable or failed or out.missing() or out.truncated:
                result.partial += 1
                result.partial_undecodable += out.undecodable
                result.partial_failed += failed
                result.partial_left_out += out.left_out
                result.partial_over_limit += out.over_limit
                result.partial_too_big += out.too_big
                result.partial_unsized += out.unsized
                result.partial_doc_limit += out.doc_limited
                result.partial_truncated += out.truncated
                log.warning("%s Seite %d nur teilweise gelesen (%d nicht lesbar, %d fehlgeschlagen, %d nicht "
                            "ausgewertet%s)", name, i, out.undecodable, failed, out.missing(),
                            ", Inhalt nicht vollständig durchsucht" if out.truncated else "")
        elif out.undecodable:
            result.undecodable += 1
        elif failed:
            result.model_failed += 1
        elif out.jobs:
            result.no_text += 1
        elif out.over_limit:
            result.too_large += 1
        elif out.too_big:
            result.too_big += 1
        elif out.unsized:
            result.unsized += 1
        elif out.doc_limited:
            result.doc_limit += 1
        elif out.truncated:
            result.too_complex += 1
        else:
            result.no_text += 1
    return result


@dataclass
class PageImages:
    """The image(s) of one PDF page for the model in the phase "Bildauswertung" (see page_images): usually
    one page image (several images of a page are put together), or why there is none."""
    number: int
    images: list = field(default_factory=list)  # (mime, data)
    problem: str | None = None  # why there is no image
    retry: bool = False  # the problem may go away (a missing component installed): try again later
    partial: str | None = None  # why some images of the page are left out (it is read only partly)
    limit: str | None = None  # the worker's limit that cut it short (see Extracted.limit)


def page_images(data: bytes, numbers: list[int], name: str) -> list[PageImages]:
    """Decode and prepare the images of the given pages of a PDF for the model, nothing else – in an
    indexing pass in the extraction worker, under its limits (the model is called by the app, see
    vision.py). The same bounds as when the pages are read (MAX_PAGE_*, MAX_DOC_PIXELS for the pages of
    one call)."""
    from pypdf import apply_configuration

    with apply_configuration(stream_parsing_work_maximum_count=PDF_STREAM_TOKENS):
        reader = _open_pdf(data)
        if reader is None:
            return [PageImages(n, problem=PDF_ENCRYPTED_TEXT) for n in numbers]
        doc, decoder = _DocState(), _Decoder()
        out_pages = []
        count = len(reader.pages)
        for n in numbers:
            _check_page()
            if not 1 <= n <= count:
                out_pages.append(PageImages(n, problem="Seite nicht vorhanden"))
                continue
            shortage = _Shortage()
            token = _SHORTAGE.set(shortage)
            try:
                page = _images_of(reader.pages[n - 1], n, name, doc, decoder)
            except MemoryError as exc:
                _out_of_memory(exc)
                page = PageImages(n, problem=f"Bilder zu groß zum Lesen ({_memory_bound()})")
            finally:
                _SHORTAGE.reset(token)
            if shortage.met and MEMORY_LIMIT_MB and (page.problem or page.partial):
                page.limit = f"memory={MEMORY_LIMIT_MB}"  # more memory may read it
            out_pages.append(page)
            _progress()
        return out_pages


def _images_of(page, number: int, name: str, doc: _DocState, decoder) -> PageImages:
    result = _ScanResult()
    try:
        drawn = _page_images(page, name, number, doc) if _can_draw_images(page, doc) else _Drawing()
    except MemoryError:
        raise
    except Exception as exc:  # noqa: BLE001 – broken resources
        log.warning("%s Seite %d: Bilder nicht lesbar (%s)", name, number, exc)
        return PageImages(number, problem=f"Bilder nicht lesbar: {_decode_problem(exc)}", retry=True)
    if not drawn.images:
        return PageImages(number, problem="kein Bild auf der Seite")
    out = _page_payloads(decoder, drawn, name, number, result, doc, 1)
    images = [(job.mime, job.data) for job in out.jobs if job.data is not None]
    if images:
        if not (out.undecodable or out.missing() or out.truncated):
            return PageImages(number, images)
        result.partial = 1
        result.partial_undecodable = out.undecodable
        result.partial_left_out = out.left_out
        result.partial_over_limit = out.over_limit
        result.partial_too_big = out.too_big
        result.partial_unsized = out.unsized
        result.partial_doc_limit = out.doc_limited
        result.partial_truncated = int(out.truncated)
        return PageImages(number, images, partial=result.partial_reasons(), retry=bool(out.undecodable))
    if out.undecodable:
        result.undecodable = 1
    elif out.over_limit:
        result.too_large = 1
    elif out.too_big:
        result.too_big = 1
    elif out.unsized:
        result.unsized = 1
    elif out.doc_limited:
        result.doc_limit = 1
    else:
        result.too_complex = 1
    return PageImages(number, problem=result.reason(), retry=result.retry)


def _page_payloads(images, drawn: _Drawing, name: str, number: int, result: _ScanResult,
                   doc: _DocState | None = None, compose_over: int = MAX_IMAGES_PER_PAGE) -> _PageOutcome:
    """The images of one page for the model (as _Job), with the numbers of images that could not be
    decoded, that were left out (more than MAX_IMAGES_PER_PAGE, not put together), that were over the
    page limits, too large to decode, without size and over the document's bounds. ``images`` decodes
    (see _Decoder).

    Each different image is decoded and sent once, however often and under whatever names the page
    draws it – and once per document: an image read on an earlier page is not decoded again. Images
    beyond MAX_PAGE_IMAGES/MAX_PAGE_PIXELS (real sizes, measured from the headers, soft masks included)
    are left out undecoded. More than MAX_IMAGES_PER_PAGE different images are put together into one page
    image (``compose_over``: more than that many – 1 puts any page of several images together); if that is
    not possible, the largest ones are sent. Every decoding is charged to the document
    with the work it took (see _MIN_DECODE_CHARGE) and checked against its bounds before it starts."""
    doc = doc if doc is not None else _DocState()
    out = _PageOutcome(truncated=drawn.truncated)
    chosen, broken = _choose(drawn, doc, out, name, number, result)
    new_jobs = 0

    def decode(image: _PageImage, target):
        """Decodes one image (charged to the document), or counts why not and returns None."""
        if image.ident in doc.failed:  # failed on an earlier page of this document
            out.undecodable += 1
            return None
        if image.ident in doc.too_big:
            out.too_big += 1
            return None
        if not doc.can_decode(image):
            out.doc_limited += 1
            return None
        try:
            decoded = images[image, target]
        except Exception as exc:  # noqa: BLE001 – missing Pillow/jbig2dec, unsupported or broken image
            doc.charge(image)
            if _too_big(exc):  # deterministic: not retried, not tried again on later pages
                log.warning("%s Seite %d: Bild zu groß zum Lesen (%s)", name, number, _decode_problem(exc))
                doc.too_big.add(image.ident)
                out.too_big += 1
                return None
            log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, number, exc)
            problem = doc.failed[image.ident] = _decode_problem(exc)
            result.decode_error = result.decode_error or problem
            out.undecodable += 1
            return None
        doc.decoded_pixels += getattr(decoded, "work", 0) or _decode_charge(image)
        return decoded

    if len(chosen) + broken > compose_over and chosen:
        layout = _layout(chosen)
        if layout is not None:
            key = ("page", tuple((image.ident, tuple(image.placements)) for image in chosen))
            if key in doc.transcripts:
                out.jobs.append(_Job(key, text=doc.transcripts[key]))
                return out
            if doc.vision_calls >= MAX_DOC_VISION_CALLS or not doc.can_decode(*chosen):
                out.doc_limited += len(chosen)
                return out
            before = (out.undecodable, out.too_big, out.doc_limited)
            try:
                combined = _compose(layout, decode)
                if combined is not None:
                    data, mime = _encode(combined)
                    complete = (out.undecodable, out.too_big, out.doc_limited) == before
                    out.jobs.append(_Job(key, data=data, mime=mime, reusable=complete))
                return out
            except Exception as exc:  # noqa: BLE001 – e.g. out of memory: fall back to the largest images
                _out_of_memory(exc)  # with more memory, the page would be read as a whole
                log.warning("%s Seite %d: Bilder nicht zusammensetzbar (%s)", name, number, exc)
                out.undecodable, out.too_big, out.doc_limited = before  # counted again below
        chosen = sorted(chosen, key=lambda image: -image.pixels)  # stable: equal sizes keep drawing order
    for k, image in enumerate(chosen):
        if len(out.jobs) >= MAX_IMAGES_PER_PAGE:
            out.left_out += len(chosen) - k
            break
        key = ("image", image.ident)
        if key in doc.transcripts:
            out.jobs.append(_Job(key, text=doc.transcripts[key]))
            continue
        if doc.vision_calls + new_jobs >= MAX_DOC_VISION_CALLS:
            out.doc_limited += 1
            continue
        decoded = decode(image, _fit(image.size, MAX_IMAGE_PIXELS))
        if decoded is None:
            continue
        try:
            data, mime = _image_payload(decoded)
        except Exception as exc:  # noqa: BLE001 – unsupported format, conversion failed
            if _too_big(exc):
                log.warning("%s Seite %d: Bild zu groß zum Lesen (%s)", name, number, _decode_problem(exc))
                doc.too_big.add(image.ident)
                out.too_big += 1
                continue
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
    size from the header, soft mask included: a mask shared by several images counts once per page) but
    not decoded – and the number of images whose data is no readable image."""
    chosen, pixels, broken = [], 0, 0
    masks: set[tuple] = set()  # masks counted on this page
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
        mask = image.mask.pixels if image.mask is not None and image.mask.ident not in masks else 0
        if problem is _DOC_LIMIT:
            out.doc_limited += 1
        elif problem is _TOO_BIG:
            if first:
                log.warning("%s Seite %d: Bild zu groß zum Lesen (%s)", name, number, _IMAGE_BOUND)
            out.too_big += 1
        elif problem is not None:
            if first:  # reported once per document
                log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, number, problem)
            result.decode_error = result.decode_error or problem
            out.undecodable += 1
            broken += 1
        elif image.size is None:
            if first:
                log.warning("%s Seite %d: Bild ohne Größenangabe nicht ausgewertet", name, number)
            out.unsized += 1
        elif pixels + image.pixels + mask > MAX_PAGE_PIXELS:
            out.over_limit += 1
        else:
            chosen.append(image)
            pixels += image.pixels + mask
            if image.mask is not None:
                masks.add(image.mask.ident)
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


# -- OpenDocument ------------------------------------------------------------------------------------------------
# An OpenDocument file (and an OpenOffice.org 1.x one) is a ZIP package; its text is in content.xml, read here
# with the standard library alone (zipfile, expat). The text comes out like that of a Word file: headings as
# Markdown headings ("#" per outline level, so chunking keeps the sections), paragraphs as lines, list items
# with "- ", table rows as their non-empty cells joined by " | ", each spreadsheet under its name as a heading,
# each slide as its own page. Footnotes and endnotes follow their paragraph ("Fußnote 1: …"); text deleted with
# change tracking, comments, tables of contents and indexes (copies of the text), embedded objects and images
# are left out. Only content.xml and – for the password check – META-INF/manifest.xml are decompressed.
#
# Bounds, all checked before the work they bound (a ZIP package decompresses to any size, and a few bytes of
# spreadsheet XML declare a million rows). A file beyond one is not read, or read in part with a note; it is
# not retried (the file will not change), but stays incomplete: a later extraction version reads it again.
# Members are only decompressed with the methods OpenDocument allows (stored, deflate): those decompress in
# small steps, so the bytes counted while reading bound the memory.
MAX_ODF_MEMBERS = 10_000  # members of the package (a document with many pictures has a few hundred)
# content.xml decompressed (a text of several hundred pages has a few MB); of a bigger one – a long list in a
# spreadsheet – the text up to the bound is read, with a note
MAX_ODF_XML_BYTES = 64_000_000
MAX_ODF_MANIFEST_BYTES = 4_000_000  # META-INF/manifest.xml decompressed (one short entry per member)
MAX_ODF_DEPTH = 100  # nesting of XML elements (LibreOffice writes 10 to 30 levels; the reading is recursive)
MAX_ODF_TEXT_CHARS = 32_000_000  # text read from one file; beyond, the rest is left out with a note
# Elements and pieces of text in one unit (paragraph, list item, table row, slide), which is held in memory
# until it ends: a list item holding a long nested list has some 100,000; a few bytes of XML per element
# (<a/>) would otherwise let a small file fill the memory. Beyond, the rest of the file is left out with a note.
MAX_ODF_UNIT_ITEMS = 500_000
# Different element and attribute names in content.xml (LibreOffice uses a few hundred): the XML parser keeps
# every name it meets for the whole file, some 300 bytes each.
MAX_ODF_NAMES = 10_000
# A non-empty spreadsheet cell or row repeated more often (table:number-columns-/number-rows-repeated) is read
# this many times, as long as the copies of one file stay within _ODF_REPEAT_CHARS characters – beyond, one
# copy (identical copies add nothing to search). Empty ones are never expanded: LibreOffice writes the unused
# rest of a sheet as one empty row repeated about a million times.
_ODF_MAX_REPEAT = 100
_ODF_REPEAT_CHARS = 1_000_000
# text:s with a larger count (text:c) gives this many spaces – fewer characters than the element has in XML,
# so the text of a paragraph never outgrows its XML; only repeated cells and rows expand (MAX_ODF_TEXT_CHARS).
_ODF_MAX_SPACES = 10
# Bytes of the ZIP central directory allowed per member (46 fixed, the name – LibreOffice's longest are some 70
# bytes –, extra fields): zipfile reads the whole directory into memory, an entry for every 46 bytes, before any
# member can be counted, so its size is checked first.
_ZIP_ENTRY_BYTES = 256
_ODF_CHUNK = 1 << 16  # decompressed bytes handed to the XML parser at a time

ODF_ENCRYPTED_TEXT = "Datei ist mit einem Passwort geschützt (verschlüsselt) – nicht gelesen"
PDF_ENCRYPTED_TEXT = ODF_ENCRYPTED_TEXT
ODF_DTD_TEXT = ("Datei nicht gelesen (XML mit eigener DTD bzw. Entity-Deklarationen – aus Sicherheitsgründen "
                "nicht erlaubt)")

# Both generations of the format, mapped to the same short prefixes: OpenDocument (ODF 1.0 to 1.4 keep these
# names) and OpenOffice.org 1.x / StarOffice 6–7 (.sxw, .sxc, .sxi).
_ODF_NAMESPACES = {
    **{f"urn:oasis:names:tc:opendocument:xmlns:{name}:1.0": prefix for name, prefix in (
        ("office", "office"), ("text", "text"), ("table", "table"), ("drawing", "draw"),
        ("presentation", "presentation"), ("style", "style"), ("svg-compatible", "svg"))},
    **{f"http://openoffice.org/2000/{name}": prefix for name, prefix in (
        ("office", "office"), ("text", "text"), ("table", "table"), ("drawing", "draw"),
        ("presentation", "presentation"), ("style", "style"))},
    "http://openoffice.org/2009/office": "office",  # LibreOffice extensions, e.g. comments on slides
    "http://www.w3.org/2000/svg": "svg",
}
# Left out with everything inside: text deleted with change tracking (the document shows the text after the
# changes), comments, generated copies of the text (tables of contents, indexes) and declarations, forms and
# scripts, embedded objects and images (with their data and alternative text), definitions of a spreadsheet
# (validation messages, named ranges, pivot tables).
_ODF_SKIP = frozenset({
    "text:tracked-changes", "table:tracked-changes", "office:annotation", "office:annotation-end",
    "text:table-of-content", "text:illustration-index", "text:table-index", "text:object-index", "text:user-index",
    "text:alphabetical-index", "text:bibliography", "text:sequence-decls", "text:variable-decls",
    "text:user-field-decls", "text:dde-connection-decls", "office:forms", "office:scripts",
    "office:event-listeners", "draw:image", "draw:object", "draw:object-ole", "office:binary-data", "svg:title",
    "svg:desc", "draw:page-thumbnail", "table:content-validations", "table:named-expressions",
    "table:named-range", "table:database-ranges", "table:data-pilot-tables", "table:dde-links",
    "table:label-ranges", "table:consolidation", "table:calculation-settings",
})
_ODF_LISTS = frozenset({"text:list", "text:ordered-list", "text:unordered-list"})  # the latter two: 1.x
_ODF_ITEMS = frozenset({"text:list-item", "text:list-header", "text:numbered-paragraph"})
_ODF_TABLES = frozenset({"table:table", "table:sub-table"})  # sub-table: tables in cells of 1.x files
# Block elements: inside a paragraph (a text frame, a shape, a note) they are read as lines of their own.
_ODF_BLOCKS = frozenset({"text:p", "text:h", "text:section", "table:table-row"}) | _ODF_LISTS | _ODF_ITEMS | _ODF_TABLES
_ODF_NOTES = frozenset({"text:note", "text:footnote", "text:endnote"})
_ODF_CITATIONS = frozenset({"text:note-citation", "text:footnote-citation", "text:endnote-citation"})
_ODF_NOTE_BODIES = frozenset({"text:note-body", "text:footnote-body", "text:endnote-body"})
# Read as a whole when they end and then dropped: paragraphs, headings, list items, table rows and slides not
# inside another one. Only these are kept in memory while content.xml is parsed.
_ODF_UNITS = frozenset({"text:p", "text:h", "table:table-row", "draw:page"}) | _ODF_ITEMS
_ODF_ATTRS = frozenset({  # attributes that are kept (the rest is formatting)
    "text:outline-level", "text:level", "text:c", "text:style-name", "text:note-class", "text:label",
    "table:name", "table:number-columns-repeated", "table:number-rows-repeated", "presentation:class",
    "office:class", "style:name", "style:family", "style:parent-style-name",
    *(f"{prefix}:{name}" for prefix in ("office", "table")  # cell values; table: in 1.x files
      for name in ("value-type", "value", "date-value", "time-value", "boolean-value", "string-value")),
})
_ODF_VALUES = {"float": "value", "percentage": "value", "currency": "value", "date": "date-value",
               "time": "time-value", "boolean": "boolean-value", "string": "string-value"}
# White space in the character data of a paragraph is collapsed (ODF 1.2, part 1, 6.1.2); explicit spaces,
# tabs and line breaks are elements. Soft hyphens are dropped.
_ODF_WHITESPACE = {9: " ", 10: " ", 13: " ", 0xAD: None}
_RUNS_OF_SPACES = re.compile(" {2,}")
_EOCD = struct.Struct("<4s4H2LH")  # ZIP end of central directory record
_EOCD64 = struct.Struct("<4sQ2H2L4Q")  # ZIP64 end of central directory record


class _OdfRefused(Exception):
    """The file is not read (a bound, a password, a DTD): ``message`` says why. ``incomplete``: a later
    extraction version may read it (a bound), else it never can."""

    def __init__(self, message: str, *, incomplete: bool = True):
        super().__init__(message)
        self.message = message
        self.incomplete = incomplete


class _OdfFull(Exception):
    """A bound reached while reading (MAX_ODF_TEXT_CHARS, MAX_ODF_UNIT_ITEMS, MAX_ODF_XML_BYTES): the text read
    so far is kept, ``note`` says what was left out; ``reason`` is the bound, for a file without any text
    before it."""

    def __init__(self, note: str, reason: str):
        super().__init__(note)
        self.note = note
        self.reason = reason


_NO_ATTRS: dict[str, str] = {}  # (shared by all elements without kept attributes – never changed)


class _Node:
    """An element of a unit being read: its short tag, the attributes in _ODF_ATTRS and its children –
    character data (str) and elements in document order."""
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str]):
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []

    def elements(self):
        return (c for c in self.children if isinstance(c, _Node))


class _Bullet:
    """The bullet of a list item ("- ", or the number the file states): shown before the item's first
    paragraph – once, also for the items it is nested in."""
    __slots__ = ("label", "outer")

    def __init__(self, label: str | None, outer: _Bullet | None):
        self.label = label
        self.outer = outer

    def take(self) -> str:
        label, bullet = self.label or "", self
        while bullet is not None:
            bullet.label, bullet = None, bullet.outer
        return label


@dataclass(frozen=True)
class _OdfContext:
    cell: bool = False  # in a table cell or a note: plain text, no heading marks or bullets
    title: bool = False  # in the title of a slide: paragraphs are headings
    bullet: _Bullet | None = None  # the list item the paragraphs belong to


_ODF_TOP = _OdfContext()
_ODF_FLAT = _OdfContext(cell=True)


class _OdfText:
    """The text of one paragraph with the white space rules of ODF: runs of white space in the character data
    – also across elements – are one space, a leading one is dropped; text:s, text:tab and text:line-break
    are kept as they are."""
    __slots__ = ("parts", "space")

    def __init__(self):
        self.parts: list[str] = []
        self.space = True

    def raw(self, text: str) -> None:
        text = _RUNS_OF_SPACES.sub(" ", text.translate(_ODF_WHITESPACE))
        if self.space and text.startswith(" "):
            text = text[1:]
        if text:
            self.parts.append(text)
            self.space = text.endswith(" ")

    def literal(self, text: str) -> None:
        self.parts.append(text)
        self.space = False

    def value(self) -> str:
        return "".join(self.parts)


def _node_text(node: _Node) -> str:
    return "".join(c if isinstance(c, str) else _node_text(c) for c in node.children)


def _flat(lines: list[str]) -> str:
    """Lines as one line: a table cell, a note."""
    return " ".join(" ".join(lines).split())


def _count(value: str | None, cap: int) -> int:
    """A count attribute (text:c, number-…-repeated): 1 if missing or invalid, at most ``cap``."""
    try:
        return max(0, min(int(value), cap)) if value is not None else 1
    except ValueError:
        return 1


def _heading_style(name: str | None) -> bool:
    """A paragraph style that makes a heading, as for Word files: Title, Heading, Überschrift … – ODF writes a
    space in a style name as "_20_"."""
    return bool(name) and name.lower().startswith(("heading", "überschrift", "title", "titel"))


def _size_text(n: int) -> str:
    return f"{_n(n // 1_000_000)} MB" if n >= 1_000_000 else f"{_n(n // 1000)} KB"


class _OdfReader:
    """Reads content.xml while it is parsed (expat): each unit (_ODF_UNITS) is collected as a small tree and
    turned into lines when it ends, then dropped – a spreadsheet with a million rows needs the memory of
    one row. Outside units only what the units need is kept (open elements, sheet name, heading styles), and
    of the elements left out (_ODF_SKIP) nothing at all, wherever they are."""

    def __init__(self):
        self.lines: list[str] = []  # text outside slides
        self.slides: list[Page] = []
        self.full: _OdfFull | None = None  # the bound reached, if any
        self._names: dict[str, str] = {}
        self._depth = 0
        self._skip = 0  # depth inside a left-out element (_ODF_SKIP): nothing of it is kept
        self._open: list[str] = []  # open elements outside units
        self._nodes: list[_Node] = []  # the open elements of the unit being read
        self._spreadsheet = False
        self._sheet: str | None = None  # name of the sheet whose rows come next, until it is written
        self._slide = 0
        self._heading_styles: set[str] = set()  # automatic paragraph styles derived from a heading style
        self._spent = 0
        self._items = 0  # elements and pieces of text of the unit being read
        self._repeat_left = _ODF_REPEAT_CHARS  # characters left for copies of repeated cells and rows

    def parser(self):
        return _odf_parser(self._start, self._end, self._data)

    def pages(self) -> list[Page]:
        text = "\n".join(self.lines).strip()
        return ([Page(None, text)] if text else []) + self.slides

    # -- parsing ------------------------------------------------------------------------------------------------
    def _tag(self, name: str) -> str:
        """``uri local`` (expat) → ``prefix:local``; an unknown namespace gives ``?:local``."""
        tag = self._names.get(name)
        if tag is None:
            if len(self._names) >= MAX_ODF_NAMES:
                raise _OdfRefused(f"Datei nicht gelesen (mehr als {_n(MAX_ODF_NAMES)} verschiedene Element- und "
                                  "Attributnamen)")
            uri, _, local = name.rpartition(" ")
            tag = self._names[name] = f"{_ODF_NAMESPACES.get(uri, '?')}:{local}" if uri else local
        return tag

    def _attrs(self, attrs: dict[str, str]) -> dict[str, str]:
        kept = {tag: value for name, value in attrs.items() if (tag := self._tag(name)) in _ODF_ATTRS}
        return kept or _NO_ATTRS

    def _start(self, name: str, attrs: dict[str, str]) -> None:
        self._depth += 1
        if self._depth > MAX_ODF_DEPTH:
            raise _OdfRefused(f"Datei nicht gelesen (mehr als {MAX_ODF_DEPTH} Ebenen verschachtelt)")
        tag = self._tag(name)  # (every name counts, also in left-out elements: MAX_ODF_NAMES)
        kept = self._attrs(attrs) if attrs else _NO_ATTRS
        if self._skip:
            self._skip += 1
            return
        if tag in _ODF_SKIP:
            self._skip = 1
        elif self._nodes:
            self._grow()
            node = _Node(tag, kept)
            self._nodes[-1].children.append(node)
            self._nodes.append(node)
        elif tag in _ODF_UNITS:
            self._items = 0
            self._nodes.append(_Node(tag, kept))
        else:
            self._container(tag, kept)

    def _grow(self) -> None:
        self._items += 1
        if self._items > MAX_ODF_UNIT_ITEMS:
            self._nodes.clear()  # (the unit is not read: its memory goes at once)
            bound = f"ein Absatz bzw. eine Zeile oder Folie mit über {_n(MAX_ODF_UNIT_ITEMS)} Elementen"
            raise _OdfFull(f"Text ab einem Absatz bzw. einer Zeile oder Folie mit über {_n(MAX_ODF_UNIT_ITEMS)} "
                           "Elementen nicht gelesen (Grenze je Abschnitt)", bound)

    def _container(self, tag: str, attrs: dict[str, str]) -> None:
        if tag == "office:spreadsheet":
            self._spreadsheet = True
        elif tag == "office:document-content":  # 1.x files name the kind here; the body has no own element
            self._spreadsheet = attrs.get("office:class") == "spreadsheet"
        elif tag in _ODF_TABLES and self._spreadsheet and not _ODF_TABLES.intersection(self._open):
            self._sheet = " ".join(attrs.get("table:name", "").split()) or None
        elif tag == "style:style" and attrs.get("style:family") == "paragraph" and attrs.get("style:name") \
                and _heading_style(attrs.get("style:parent-style-name")):
            self._heading_styles.add(attrs["style:name"])
        self._open.append(tag)

    def _end(self, name: str) -> None:
        self._depth -= 1
        if self._skip:
            self._skip -= 1
        elif self._nodes:
            node = self._nodes.pop()
            if not self._nodes:
                self._unit(node)
        elif self._open:
            tag = self._open.pop()
            if tag in _ODF_TABLES and not _ODF_TABLES.intersection(self._open):
                self._sheet = None  # a sheet without rows: no heading

    def _data(self, text: str) -> None:
        if self._nodes and not self._skip:
            self._grow()
            self._nodes[-1].children.append(text)

    def _spend(self, chars: int) -> None:
        self._spent += chars
        if self._spent > MAX_ODF_TEXT_CHARS:
            raise _OdfFull(f"Text nach {_n(MAX_ODF_TEXT_CHARS)} Zeichen gekürzt (Grenze je Datei)",
                           f"Text über {_n(MAX_ODF_TEXT_CHARS)} Zeichen")

    def _copies(self, text: str, attr: str | None) -> int:
        """How often a repeated cell or row is written: as often as the file says, at most _ODF_MAX_REPEAT
        times, and once when the copies of the file would pass _ODF_REPEAT_CHARS."""
        n = max(1, _count(attr, _ODF_MAX_REPEAT))
        if n > 1:
            extra = len(text) * (n - 1)
            if extra > self._repeat_left:
                return 1
            self._repeat_left -= extra
        return n

    # -- units --------------------------------------------------------------------------------------------------
    def _unit(self, node: _Node) -> None:
        lines: list[str] = []
        if node.tag == "draw:page":  # a slide (or a page of a drawing): one page with its number
            self._slide += 1
            notes: list[str] = []
            for child in node.elements():
                self._block(child, _ODF_TOP, notes if child.tag == "presentation:notes" else lines)
            if notes:
                lines += ["Notizen:", *notes]
            for line in lines:
                self._spend(len(line) + 1)
            text = "\n".join(lines).strip()
            if text:
                self.slides.append(Page(self._slide, text))
            return
        self._block(node, _ODF_TOP, lines)
        if lines and self._sheet:
            lines.insert(0, f"\n# {self._sheet}")
            self._sheet = None
        for line in lines:
            self._spend(len(line) + 1)
            self.lines.append(line)

    def _block(self, node: _Node, ctx: _OdfContext, lines: list[str]) -> None:
        tag = node.tag
        if tag in ("text:p", "text:h"):
            self._paragraph(node, ctx, lines)
        elif tag in _ODF_ITEMS:
            self._item(node, ctx, lines)
        elif tag == "table:table-row":
            self._row(node, lines)
        else:  # lists, tables, sections, frames, shapes: what they contain
            if node.attrs.get("presentation:class") == "title" and not ctx.cell:
                ctx = replace(ctx, title=True)
            for child in node.elements():
                self._block(child, ctx, lines)

    def _item(self, node: _Node, ctx: _OdfContext, lines: list[str]) -> None:
        label = None
        if node.tag != "text:list-header":  # a list header has no bullet
            number = next((c for c in node.elements() if c.tag == "text:number"), None)
            stated = " ".join(_node_text(number).split()) if number is not None else ""
            label = f"{stated} " if stated else "- "
        inner = replace(ctx, bullet=_Bullet(label, ctx.bullet))
        for child in node.elements():
            if child.tag != "text:number":
                self._block(child, inner, lines)

    def _paragraph(self, node: _Node, ctx: _OdfContext, lines: list[str]) -> None:
        notes: list[str] = []
        boxes: list[str] = []  # text frames, shapes and the like inside the paragraph: lines of their own
        out = _OdfText()
        self._inline(node, ctx, out, notes, boxes)
        text = out.value()
        if ctx.cell:
            lines += [text, *boxes, *notes]
            return
        level = self._heading_level(node, ctx)
        if level:
            text = " ".join(text.split())
            if text:
                if ctx.bullet:
                    ctx.bullet.take()
                lines.append(f"\n{'#' * level} {text}")
        else:
            text = text.strip()
            if text:
                lines.append((ctx.bullet.take() if ctx.bullet else "") + text)
        lines += boxes + notes

    def _heading_level(self, node: _Node, ctx: _OdfContext) -> int:
        if node.tag == "text:h":  # text:level in 1.x files; 1 if missing
            return _count(node.attrs.get("text:outline-level") or node.attrs.get("text:level"), 6) or 1
        style = node.attrs.get("text:style-name")
        return 1 if ctx.title or style in self._heading_styles or _heading_style(style) else 0

    def _inline(self, node: _Node, ctx: _OdfContext, out: _OdfText, notes: list[str], boxes: list[str]) -> None:
        for child in node.children:
            if isinstance(child, str):
                out.raw(child)
                continue
            tag = child.tag
            if tag in _ODF_CITATIONS:  # outside a note: no text of its own
                continue
            if tag == "text:s":
                out.literal(" " * _count(child.attrs.get("text:c"), _ODF_MAX_SPACES))
            elif tag in ("text:tab", "text:tab-stop"):  # tab-stop: 1.x files
                out.literal("\t")
            elif tag == "text:line-break":
                out.literal("\n")
            elif tag in _ODF_NOTES:
                note = self._note(child)
                if note:
                    notes.append(note)
            elif tag == "text:number":  # the number of a heading as the file states it
                out.raw(" ".join(_node_text(child).split()) + " ")
            elif tag in _ODF_BLOCKS:
                self._block(child, _ODF_FLAT if ctx.cell else _ODF_TOP, boxes)
            else:  # spans, links, fields (their shown value), frames and shapes
                self._inline(child, ctx, out, notes, boxes)

    def _note(self, node: _Node) -> str | None:
        """'Fußnote 1: …' / 'Endnote i: …' – kept after the paragraph, not glued to the word it marks."""
        label, lines = "", []
        for child in node.elements():
            if child.tag in _ODF_CITATIONS:
                label = " ".join(_node_text(child).split()) or child.attrs.get("text:label", "")
            elif child.tag in _ODF_NOTE_BODIES:
                for block in child.elements():
                    self._block(block, _ODF_FLAT, lines)
        text = _flat(lines)
        if not text:
            return None
        endnote = node.attrs.get("text:note-class", node.tag) in ("endnote", "text:endnote")
        kind = "Endnote" if endnote else "Fußnote"
        return f"{kind} {label}: {text}" if label else f"{kind}: {text}"

    def _row(self, node: _Node, lines: list[str]) -> None:
        """A table row as its non-empty cells joined by " | " (as for Word files). Cells hidden under a merged
        one are left out; repetitions of non-empty cells and rows count (:meth:`_copies`)."""
        cells: list[str] = []
        for cell in node.elements():
            if cell.tag != "table:table-cell":  # covered-table-cell: hidden under a merged cell
                continue
            text = self._cell(cell)
            if text:
                cells += [text] * self._copies(text, cell.attrs.get("table:number-columns-repeated"))
        if cells:
            line = " | ".join(cells)
            lines += [line] * self._copies(line, node.attrs.get("table:number-rows-repeated"))

    def _cell(self, cell: _Node) -> str:
        """The text a cell shows (for a formula its result, as stored by the program that saved the file); a
        cell with a value but without text (files written by other programs) gives the value."""
        lines: list[str] = []
        for child in cell.elements():
            self._block(child, _ODF_FLAT, lines)
        text = _flat(lines)
        if text:
            return text
        key = _ODF_VALUES.get(cell.attrs.get("office:value-type") or cell.attrs.get("table:value-type") or "")
        return (cell.attrs.get(f"office:{key}") or cell.attrs.get(f"table:{key}") or "").strip() if key else ""


def _odf_doctype(name, system_id, public_id, has_internal_subset) -> None:
    # 1.x files name an external DTD (office.dtd), which is never loaded; an internal subset can declare
    # entities (billion laughs, external files) – LibreOffice never writes one
    if has_internal_subset:
        raise _OdfRefused(ODF_DTD_TEXT, incomplete=False)


def _odf_entity(*_args) -> None:
    raise _OdfRefused(ODF_DTD_TEXT, incomplete=False)


def _odf_parser(start, end=None, data=None):
    """An expat parser with namespaces ("uri local" names) that refuses DTDs with an internal subset and any
    entity declaration, and never reads external entities."""
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.buffer_text = True
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartDoctypeDeclHandler = _odf_doctype
    parser.EntityDeclHandler = _odf_entity
    parser.UnparsedEntityDeclHandler = _odf_entity
    parser.ExternalEntityRefHandler = _odf_entity
    parser.StartElementHandler = start
    if end is not None:
        parser.EndElementHandler = end
    if data is not None:
        parser.CharacterDataHandler = data
    return parser


def _check_zip_directory(data: bytes) -> None:
    """Refuse a package with too many members before zipfile reads its central directory (into memory, a few
    hundred bytes per member, whatever their number). The directory is located as zipfile locates it: the
    end record at the very end, else the last one within the trailing comment, and the ZIP64 record before
    it if there is one. Without an end record zipfile reports the file as broken."""
    end = len(data) - _EOCD.size
    if end < 0:
        return
    if not (data[end:end + 4] == b"PK\x05\x06" and data[-2:] == b"\0\0"):
        # (zipfile looks back over the longest comment, 64 KB, and the end record)
        end = data.rfind(b"PK\x05\x06", max(0, len(data) - _EOCD.size - (1 << 16)))
        if end < 0 or end + _EOCD.size > len(data):
            return
    entries, size = _EOCD.unpack_from(data, end)[4:6]
    if end >= 20 and data[end - 20:end - 16] == b"PK\x06\x07":
        # a ZIP64 end record: directly before its locator, or where the locator says (Python versions differ)
        for record in {end - 20 - _EOCD64.size, struct.unpack_from("<Q", data, end - 12)[0]}:
            if 0 <= record <= len(data) - _EOCD64.size and data[record:record + 4] == b"PK\x06\x06":
                zip64 = _EOCD64.unpack_from(data, record)
                entries, size = max(entries, zip64[7]), max(size, zip64[8])
    if entries > MAX_ODF_MEMBERS or size > MAX_ODF_MEMBERS * _ZIP_ENTRY_BYTES:
        raise _OdfRefused(f"Datei zu groß zum Lesen (mehr als {_n(MAX_ODF_MEMBERS)} Bestandteile)")


def _parse_member(package, info, limit: int, what: str, parser, *, partial: bool = False) -> None:
    """Decompress one member into the parser, at most ``limit`` bytes – counted while reading (a package may
    state a wrong size). Beyond, a member read ``partial`` ends with what was read so far (_OdfFull), any
    other is refused – before it is decompressed, if the package states its size. Only the compression
    methods OpenDocument allows are read: with others (bzip2, LZMA) zipfile would decompress a whole block of
    any size at once."""
    if getattr(info, "compress_type", zipfile.ZIP_DEFLATED) not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise _OdfRefused(f"Datei nicht gelesen ({what} mit einer Kompression, die OpenDocument nicht kennt)",
                          incomplete=False)
    too_big = f"Datei zu groß zum Lesen ({what} entpackt über {_size_text(limit)})"
    if info.file_size > limit and not partial:
        raise _OdfRefused(too_big)
    read = 0
    with package.open(info) as stream:
        while chunk := stream.read(_ODF_CHUNK):
            if read + len(chunk) > limit:
                if not partial:
                    raise _OdfRefused(too_big)
                parser.Parse(chunk[:limit - read], False)
                raise _OdfFull(f"{what} entpackt über {_size_text(limit)} – nur der Anfang gelesen",
                               f"{what} entpackt über {_size_text(limit)}")
            read += len(chunk)
            _check_page()
            parser.Parse(chunk, False)
    parser.Parse(b"", True)


def _odf_encrypted(package, members: dict) -> bool:
    """Is the document protected with a password? ODF encrypts the members, not the ZIP package; the manifest
    names the encrypted ones. content.xml encrypted – or missing while others are (ODF 1.4 encrypts the whole
    package into one member). A password only on macros does not count. A manifest that cannot be read (broken
    XML, broken compression, too big) says nothing: content.xml decides."""
    info = members.get("META-INF/manifest.xml")
    if info is None:
        return False
    if info.flag_bits & 0x1:  # encrypted as a ZIP package (not ODF's way, but a password all the same)
        return True
    encrypted: set[str] = set()
    entry: list[str | None] = [None]

    def start(name: str, attrs: dict[str, str]) -> None:
        local = name.rpartition(" ")[2]
        if local == "file-entry":
            entry[0] = next((v for k, v in attrs.items() if k.rpartition(" ")[2] == "full-path"), None)
        elif local == "encryption-data" and entry[0] is not None:
            encrypted.add(entry[0])

    try:
        _parse_member(package, info, MAX_ODF_MANIFEST_BYTES, "Verzeichnis", _odf_parser(start))
    except _OdfRefused as refused:
        if refused.message == ODF_DTD_TEXT:  # entity declarations: refused wherever they are
            raise
        return False  # too big, an unknown compression: says nothing
    except (expat.ExpatError, zipfile.BadZipFile, zlib.error, EOFError, ValueError):
        return False  # a damaged manifest says nothing
    return "content.xml" in encrypted or ("content.xml" not in members and bool(encrypted))


def _read_odf(data: bytes) -> _OdfReader:
    _check_zip_directory(data)
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        infos = package.infolist()
        if len(infos) > MAX_ODF_MEMBERS:
            raise _OdfRefused(f"Datei zu groß zum Lesen (mehr als {_n(MAX_ODF_MEMBERS)} Bestandteile)")
        members = {i.filename: i for i in infos}  # the last of equal names, as zipfile.getinfo
        content = members.get("content.xml")
        if (content is not None and content.flag_bits & 0x1) or _odf_encrypted(package, members):
            raise _OdfRefused(ODF_ENCRYPTED_TEXT, incomplete=False)
        if content is None:
            raise ValueError("content.xml fehlt – keine OpenDocument-Datei")
        reader = _OdfReader()
        try:
            _parse_member(package, content, MAX_ODF_XML_BYTES, "Inhalt", reader.parser(), partial=True)
        except _OdfFull as full:
            reader.full = full
    return reader


def _odf(data: bytes) -> Extracted:
    """OpenDocument and OpenOffice.org 1.x files (see above). A broken package or broken XML raises – recorded
    like any broken file; bounds, passwords and DTDs give a result that is not retried."""
    try:
        reader = _read_odf(data)
    except _OdfRefused as refused:
        return Extracted([], status="skipped", message=refused.message, incomplete=refused.incomplete)
    if reader.full:  # not retried (the file will not change); a later extraction version reads it again
        pages = reader.pages()
        if not pages:
            return Extracted([], status="skipped", message=f"Datei zu groß zum Lesen ({reader.full.reason})",
                             incomplete=True)
        return Extracted(pages, incomplete=True, message=reader.full.note)
    return Extracted(reader.pages())


def _image(data: bytes, ext: str, llm, *, vision: bool, defer: bool, ocr: bool) -> Extracted:
    """An image file (plan, photo, scan): Tesseract's text if ``ocr``, the model's – right away or, with
    ``defer``, later (Extracted.vision_pages) – with Tesseract's text as a hint."""
    known = ocr_image(data) if ocr else ""
    if not vision:
        if known:
            return Extracted([Page(1, known)], incomplete=True,
                             message=f"Bild per Texterkennung gelesen – {_VISION_OFF}")
        return Extracted([], status="skipped", incomplete=True, message=f"Bild – {_VISION_OFF}")
    if defer:
        if known:
            return Extracted([Page(1, known)], vision_pages=[1])
        return Extracted([], status="skipped", vision_pages=[1])
    text = llm.describe_image(data, IMAGE_EXT[ext], vision_prompt(known)).text or ""
    if not text.strip() and known:
        return Extracted([Page(1, known)], incomplete=True, message="Bildauswertung ergab keinen Text")
    return Extracted([Page(1, text)], method="vision", incomplete=not text.strip())


def extract(path: Path | str, data: bytes | None = None, *, llm=None, defer_vision: bool = False,
            ocr: bool | None = None, recheck: bool = False, vision: bool | None = None) -> Extracted:
    """Extract text from a file. ``data`` may be given (mail attachments); the file is only read.

    ``defer_vision``: the model is not called – scan pages (and images) are listed in
    ``Extracted.vision_pages`` for the phase "Bildauswertung", with their text layer or Tesseract's text for
    now. ``ocr``: read pages without a text layer (and images) with Tesseract first (default: if it is
    installed). ``recheck``: see _pdf_pages. ``vision``: whether vision is on, if not from ``llm``.

    Within a memory limit (MEMORY_LIMIT_MB, the extraction worker), an incomplete result of an extraction
    that ran out of memory names the limit (Extracted.limit): a higher limit reads the file again."""
    shortage = _Shortage()
    token = _SHORTAGE.set(shortage)
    if vision is None:
        vision = llm is not None and getattr(llm, "vision", False)
    ocr = ocr_available() if ocr is None else ocr
    try:
        result = _extract(Path(path), data, llm, defer=defer_vision and vision, ocr=ocr, recheck=recheck,
                          vision=vision)
    finally:
        _SHORTAGE.reset(token)
    if shortage.met and MEMORY_LIMIT_MB and result.incomplete:
        result.limit = f"memory={MEMORY_LIMIT_MB}"
    return result


def _extract(path: Path, data: bytes | None, llm, *, defer: bool = False, ocr: bool = False,
             recheck: bool = False, vision: bool = False) -> Extracted:
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXT:
        return Extracted([], status="skipped", message=f"Format {ext or '(ohne Endung)'} nicht unterstützt")
    try:
        if data is None:
            data = path.read_bytes()
        if ext in TEXT_EXT:
            result = Extracted([Page(None, decode_text(data))])
        elif ext == ".pdf":
            result = _pdf(data, llm, path.name, vision=vision, defer=defer, ocr=ocr, recheck=recheck)
        elif ext == ".docx":
            result = _docx(data)
        elif ext in ODF_EXT:
            result = _odf(data)
        elif ext == ".eml":
            result = _eml(data)
        else:  # images: plans, photos, scans
            result = _image(data, ext, llm, vision=vision, defer=defer, ocr=ocr)
            if result.status != "ok":
                return result
    except MemoryError as exc:  # within the worker's memory limit (EXTRACT_MEMORY_MB): it will not fit later
        _out_of_memory(exc)
        result = None  # (handled below: here the exception still holds the reading's memory)
    except Exception as exc:  # noqa: BLE001 – one broken file must not stop indexing
        log.warning("Extraktion fehlgeschlagen: %s (%s)", path.name, exc)
        return Extracted([], status="error", message=f"{type(exc).__name__}: {exc}"[:300],
                         incomplete=True, retry=True)
    if result is None:  # out of memory – past the except block, so what the reading held is freed
        log.warning("Extraktion abgebrochen: %s (%s)", path.name, _memory_bound())
        return too_big_to_read()
    if result.status == "ok" and not result.text.strip():
        result.status, result.message = "empty", "kein Text gefunden"
    return result
