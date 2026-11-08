"""Text extraction: classic extraction first, the multimodal model only as a fallback for scans
and plans (and only with LLM_VISION=1). Without vision, such files are recorded as skipped and
marked incomplete, so they are read once vision is switched on (see documents.reprocess_due)."""
from __future__ import annotations

import codecs
import email
import io
import logging
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
EXTRACT_VERSION = 2
# Images of one PDF page that are sent to the model (scanners write one image per page; more are
# usually logos or decoration).
MAX_IMAGES_PER_PAGE = 3
# Larger page images are scaled down before they are sent (an A0 plan scanned at 300 dpi has about
# 140 megapixels; multimodal models work with far fewer and requests must stay reasonably small).
MAX_IMAGE_PIXELS = 16_000_000
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
        message = f"{unread} Seite(n) ohne Textebene nicht gelesen ({scan.reason()})" if unread else None
        return Extracted(pages, method=method, message=message, incomplete=unread > 0, retry=scan.retry)
    if not unread:  # only blank pages – nothing a model could read
        return Extracted([], status="empty", message="kein Text gefunden")
    return Extracted([], status="error" if scan.retry else "skipped",
                     message=f"Scan ohne Textebene – {scan.reason()}", incomplete=True, retry=scan.retry)


@dataclass
class _ScanResult:
    """Outcome of reading the pages without a text layer with the multimodal model."""
    pages: list[Page] = field(default_factory=list)
    no_text: int = 0  # the model read the page images but answered nothing
    undecodable: int = 0  # page images could not be decoded (Pillow/jbig2dec missing, broken image)
    model_failed: int = 0  # the model call failed (timeout, server down)
    decode_error: str = ""  # first decoding problem, for the message

    @property
    def unread(self) -> int:
        return self.no_text + self.undecodable + self.model_failed

    @property
    def retry(self) -> bool:
        """A failure that may go away (model back, missing component installed): try again later."""
        return bool(self.undecodable or self.model_failed)

    def reason(self) -> str:
        parts = []
        if self.undecodable:
            parts.append(f"Bilder nicht lesbar: {self.decode_error}" if self.decode_error else "Bilder nicht lesbar")
        if self.model_failed:
            parts.append("Bildauswertung fehlgeschlagen")
        if self.no_text:
            parts.append("Bildauswertung ergab keinen Text")
        if len(parts) > 1:  # say which reason applies to how many pages
            counts = [n for n in (self.undecodable, self.model_failed, self.no_text) if n]
            parts = [f"{n}× {p}" for n, p in zip(counts, parts)]
        return "; ".join(parts)


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


def _image_payload(image) -> tuple[bytes, str]:
    """Bytes and MIME type of a PDF page image in a format multimodal models accept (JPEG/PNG).

    JPEG and PNG within the size limit are sent as they are; other formats (JPEG 2000, TIFF from
    CCITT fax or LZW, CMYK) and very large images are converted with Pillow. Raises if the image
    cannot be decoded."""
    ext = Path(getattr(image, "name", "") or "").suffix.lower()
    pil = getattr(image, "image", None)
    mime = _SENDABLE.get(ext)
    if pil is None:
        if mime and image.data:
            return image.data, mime
        raise ValueError(f"Bildformat {ext or '(unbekannt)'} nicht unterstützt")
    width, height = pil.size
    fits = width * height <= MAX_IMAGE_PIXELS
    if fits and mime == "image/png":
        return image.data, mime
    if fits and mime == "image/jpeg" and pil.mode in ("L", "RGB"):
        return image.data, mime
    from PIL import Image  # installed with requirements.txt; pypdf needs it for images anyway

    img = pil
    if img.mode not in ("1", "L", "RGB"):
        img = img.convert("RGB")  # CMYK, palette, alpha channel, 16 bit
    if not fits:
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


def _read_scanned_pages(empty_pages, llm, name: str) -> _ScanResult:
    """Let the multimodal model read the images of pages without a text layer.

    Pages without any image (blank pages) count as nothing to read. A page counts as undecodable or
    failed – and the document is retried later – if one of its images could not be decoded or the
    model call failed and nothing else on the page was read; only a page whose images the model read
    without finding text counts as "no text"."""
    result = _ScanResult()
    for i, page in empty_pages:
        decode_failed = model_failed = False
        texts = []
        try:
            images = page.images  # decoded one by one below, so one broken image spoils nothing else
            keys = list(images.keys())
        except Exception as exc:  # noqa: BLE001 – broken resources: the page cannot be read now
            log.warning("%s Seite %d: Bilder nicht lesbar (%s)", name, i, exc)
            result.decode_error = result.decode_error or _decode_problem(exc)
            result.undecodable += 1
            continue
        if not keys:
            continue
        for key in keys[:MAX_IMAGES_PER_PAGE]:
            try:
                data, mime = _image_payload(images[key])
            except Exception as exc:  # noqa: BLE001 – missing Pillow/jbig2dec, unsupported or broken image
                log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, i, exc)
                result.decode_error = result.decode_error or _decode_problem(exc)
                decode_failed = True
                continue
            try:
                text = llm.describe_image(data, mime, VISION_PROMPT).text
            except Exception as exc:  # noqa: BLE001
                log.warning("%s Seite %d: Bildauswertung fehlgeschlagen (%s)", name, i, exc)
                model_failed = True
                continue
            if text and text.strip():
                texts.append(text)
        if texts:
            result.pages.append(Page(i, "\n".join(texts)))
        elif decode_failed:
            result.undecodable += 1
        elif model_failed:
            result.model_failed += 1
        else:
            result.no_text += 1
    return result


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
        try:
            text = body.get_content()
        except (LookupError, UnicodeDecodeError):
            text = decode_text(body.get_payload(decode=True) or b"")
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
