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
# 3: pages with more images than MAX_IMAGES_PER_PAGE (strip scans) are put together into one page
#    image; pages read only partly are marked incomplete – documents of version 2 are read again.
EXTRACT_VERSION = 3
# Images of one PDF page that are sent to the model one by one (scanners write one image per page).
# A page with more images – scanners and PDF tools that cut a page into strips or tiles – is put
# together into one image at the images' positions on the page and sent as that.
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


@dataclass
class _ScanResult:
    """Outcome of reading the pages without a text layer with the multimodal model."""
    pages: list[Page] = field(default_factory=list)
    no_text: int = 0  # the model read the page images but answered nothing
    undecodable: int = 0  # page images could not be decoded (Pillow/jbig2dec missing, broken image)
    model_failed: int = 0  # the model call failed (timeout, server down)
    decode_error: str = ""  # first decoding problem, for the message
    # pages read only partly: some of their images could not be decoded, their model call failed, or
    # they could not be put together into one page image and were left out (counts of images)
    partial: int = 0
    partial_undecodable: int = 0
    partial_failed: int = 0
    partial_left_out: int = 0

    @property
    def unread(self) -> int:
        return self.no_text + self.undecodable + self.model_failed

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
        return f"{self.partial} Seite(n) nur teilweise gelesen ({'; '.join(parts)})"

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


# -- images as drawn on a page -------------------------------------------------------------------------
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
MAX_FORM_DEPTH = 6


def _mul(m, n) -> tuple[float, ...]:
    """PDF matrix product m × n ([a b c d e f])."""
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D, e * A + f * C + E, e * B + f * D + F)


def _drawn_images(page) -> list[tuple[object, tuple[float, ...]]]:
    """The images a page actually draws, in drawing order: (key for ``page.images``, transformation
    matrix). Follows q/Q/cm and form XObjects (keys as pypdf names them: "/Im1", ["/Fm1", "/Im1"],
    inline images "~0~"). Resources listed but never drawn (some PDF tools share one resource
    dictionary between all pages) are not included. Raises if the content cannot be followed."""
    from pypdf.generic import ContentStream

    found: list[tuple[object, tuple[float, ...]]] = []
    inline = 0

    def walk(content, resources, ctm, path: list[str], seen: tuple) -> None:
        nonlocal inline
        stack: list[tuple[float, ...]] = []
        xobjects = resources.get("/XObject") if resources is not None else None
        xobjects = xobjects.get_object() if xobjects is not None else None
        for operands, op in content.operations:
            if op == b"q":
                stack.append(ctm)
            elif op == b"Q":
                ctm = stack.pop() if stack else ctm
            elif op == b"cm" and len(operands) == 6:
                ctm = _mul(tuple(float(x) for x in operands), ctm)
            elif op == b"INLINE IMAGE":
                if not path:  # pypdf only lists inline images of the page itself
                    found.append((f"~{inline}~", ctm))
                    inline += 1
            elif op == b"Do" and operands and xobjects is not None:
                name = str(operands[0])
                xobj = xobjects.get(name)
                xobj = xobj.get_object() if xobj is not None else None
                if xobj is None:
                    continue
                subtype = xobj.get("/Subtype")
                if subtype == "/Image":
                    found.append(((name if not path else [*path, name]), ctm))
                elif subtype == "/Form" and len(path) < MAX_FORM_DEPTH:
                    ref = getattr(xobj, "indirect_reference", None)
                    if ref is not None and ref in seen:
                        continue
                    matrix = tuple(float(x) for x in xobj.get("/Matrix", _IDENTITY))
                    inner = xobj.get("/Resources")
                    walk(ContentStream(xobj, page.pdf), inner.get_object() if inner is not None else resources,
                         _mul(matrix, ctm), [*path, name], (*seen, ref))

    content = page.get_contents()
    if content is None:
        return found
    resources = page.get("/Resources")
    walk(content, resources.get_object() if resources is not None else None, _IDENTITY, [], ())
    return found


def _corners(ctm) -> dict[str, tuple[float, float]]:
    """Where the image's top-left, top-right and bottom-left corners land on the page (points)."""
    a, b, c, d, e, f = ctm
    return {"tl": (c + e, d + f), "tr": (a + c + e, b + d + f), "bl": (e, f)}


def _compose(placed: list[tuple[object, tuple[float, ...]]]):
    """Put the images of a page together at their positions into one page image (Pillow), or None
    when that is not possible (skewed or empty placement). ``placed``: (Pillow image, matrix)."""
    from PIL import Image

    boxes = []
    scale = 0.0
    for img, ctm in placed:
        k = _corners(ctm)
        right = (k["tr"][0] - k["tl"][0], k["tr"][1] - k["tl"][1])
        down = (k["bl"][0] - k["tl"][0], k["bl"][1] - k["tl"][1])
        straight = abs(right[1]) < 1e-6 * max(1.0, abs(right[0])) and abs(down[0]) < 1e-6 * max(1.0, abs(down[1]))
        turned = abs(right[0]) < 1e-6 * max(1.0, abs(right[1])) and abs(down[1]) < 1e-6 * max(1.0, abs(down[0]))
        if not (straight or turned):
            return None  # skewed or rotated by an odd angle
        width_pt = abs(right[0]) + abs(down[0])
        height_pt = abs(right[1]) + abs(down[1])
        if width_pt < 1e-3 or height_pt < 1e-3:
            return None
        xs = [p[0] for p in k.values()] + [k["tr"][0] + down[0]]
        ys = [p[1] for p in k.values()] + [k["tr"][1] + down[1]]
        boxes.append((img, right, down, turned, min(xs), max(ys), width_pt, height_pt))
        pixels_wide = img.size[1] if turned else img.size[0]
        scale = max(scale, pixels_wide / width_pt)
    left = min(b[4] for b in boxes)
    top = max(b[5] for b in boxes)
    right_edge = max(b[4] + b[6] for b in boxes)
    bottom = min(b[5] - b[7] for b in boxes)
    w_pt, h_pt = right_edge - left, top - bottom
    scale = min(scale, (MAX_IMAGE_PIXELS / (w_pt * h_pt)) ** 0.5)
    size = (max(1, round(w_pt * scale)), max(1, round(h_pt * scale)))
    colour = any(img.mode not in ("1", "L", "LA", "I", "I;16", "F") for img, *_ in boxes)
    canvas = Image.new("RGB" if colour else "L", size, "white")
    for img, right, down, turned, x0, y0, wp, hp in boxes:
        if turned:  # image columns run down the page: swap axes first
            img = img.transpose(Image.Transpose.TRANSPOSE)
            right, down = down, right
        if right[0] < 0:
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if down[1] > 0:  # PDF y grows upwards: the image's rows run up the page
            img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        target = (max(1, round(wp * scale)), max(1, round(hp * scale)))
        mask = None
        if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
            img = img.convert("RGBA")
            mask = img.getchannel("A").resize(target, Image.Resampling.BILINEAR)
        if img.mode == "1":
            img = img.convert("L")  # scaling bilevel scans keeps text readable in grey
        img = img.convert(canvas.mode).resize(target, Image.Resampling.LANCZOS)
        canvas.paste(img, (round((x0 - left) * scale), round((top - y0) * scale)), mask)
    return canvas


def _read_scanned_pages(empty_pages, llm, name: str) -> _ScanResult:
    """Let the multimodal model read the images of pages without a text layer.

    Up to MAX_IMAGES_PER_PAGE images of a page are sent one by one; a page with more (strip or tile
    scans) is put together into one page image at the images' positions. Pages without any image
    (blank pages) count as nothing to read. A page none of whose images could be read counts as
    undecodable or failed – the document is retried later; only a page whose images the model read
    without finding text counts as "no text". A page of which only some images were read counts as
    read *partly*: its text is kept and the document stays incomplete (and is retried when the cause
    may go away)."""
    result = _ScanResult()
    for i, page in empty_pages:
        try:
            images = page.images  # decoded one by one below, so one broken image spoils nothing else
            try:
                drawn = _drawn_images(page)
            except Exception as exc:  # noqa: BLE001 – unusual content stream: all listed images, unplaced
                log.debug("%s Seite %d: Bildpositionen nicht bestimmbar (%s)", name, i, exc)
                drawn = []
            listed = list(images.keys())
            if not drawn and listed:
                drawn = [(key, None) for key in listed]
        except Exception as exc:  # noqa: BLE001 – broken resources: the page cannot be read now
            log.warning("%s Seite %d: Bilder nicht lesbar (%s)", name, i, exc)
            result.decode_error = result.decode_error or _decode_problem(exc)
            result.undecodable += 1
            continue
        if not drawn:
            continue
        payloads, undecodable, left_out = _page_payloads(images, drawn, name, i, result)
        texts, failed = [], 0
        for data, mime in payloads:
            try:
                text = llm.describe_image(data, mime, VISION_PROMPT).text
            except Exception as exc:  # noqa: BLE001
                log.warning("%s Seite %d: Bildauswertung fehlgeschlagen (%s)", name, i, exc)
                failed += 1
                continue
            if text and text.strip():
                texts.append(text)
        if texts:
            result.pages.append(Page(i, "\n".join(texts)))
            if undecodable or failed or left_out:
                result.partial += 1
                result.partial_undecodable += undecodable
                result.partial_failed += failed
                result.partial_left_out += left_out
                log.warning("%s Seite %d nur teilweise gelesen (%d nicht lesbar, %d fehlgeschlagen, %d nicht "
                            "ausgewertet)", name, i, undecodable, failed, left_out)
        elif undecodable:
            result.undecodable += 1
        elif failed:
            result.model_failed += 1
        else:
            result.no_text += 1
    return result


def _page_payloads(images, drawn, name: str, number: int, result: _ScanResult):
    """The images of one page as (bytes, MIME type) for the model, plus the number of images that
    could not be decoded and the number left out. More than MAX_IMAGES_PER_PAGE images are put
    together into one page image; if that is not possible, the largest ones are sent."""
    decoded, undecodable = [], 0
    for key, ctm in drawn:
        try:
            decoded.append((images[key], ctm))
        except Exception as exc:  # noqa: BLE001 – missing Pillow/jbig2dec, unsupported or broken image
            log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, number, exc)
            result.decode_error = result.decode_error or _decode_problem(exc)
            undecodable += 1
    if len(drawn) > MAX_IMAGES_PER_PAGE and decoded:
        combined = None
        if all(ctm is not None and getattr(image, "image", None) is not None for image, ctm in decoded):
            try:
                combined = _compose([(image.image, ctm) for image, ctm in decoded])
            except Exception as exc:  # noqa: BLE001 – e.g. out of memory: fall back to the largest images
                log.warning("%s Seite %d: Bilder nicht zusammensetzbar (%s)", name, number, exc)
        if combined is not None:
            try:
                return [_encode(combined)], undecodable, 0
            except Exception as exc:  # noqa: BLE001
                log.warning("%s Seite %d: Seitenbild nicht erzeugbar (%s)", name, number, exc)
        decoded.sort(key=lambda x: -_pixels(x[0]))  # stable: equal sizes keep drawing order
    payloads = []
    for image, _ctm in decoded[:MAX_IMAGES_PER_PAGE]:
        try:
            payloads.append(_image_payload(image))
        except Exception as exc:  # noqa: BLE001 – unsupported format, conversion failed
            log.warning("%s Seite %d: Bild nicht lesbar (%s)", name, number, exc)
            result.decode_error = result.decode_error or _decode_problem(exc)
            undecodable += 1
    return payloads, undecodable, max(0, len(decoded) - MAX_IMAGES_PER_PAGE)


def _pixels(image) -> int:
    pil = getattr(image, "image", None)
    return pil.size[0] * pil.size[1] if pil is not None else 0


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
