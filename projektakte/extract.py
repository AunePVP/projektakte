"""Text extraction: classic extraction first, the multimodal model only as a fallback for scans
and plans (and only with LLM_VISION=1). Without vision, such files are recorded as skipped."""
from __future__ import annotations

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

    @property
    def text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)


def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
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
    if llm is not None and getattr(llm, "vision", False):
        method = "text"
        for i, page in empty_pages:
            texts = []
            try:
                images = list(page.images)[:3]
            except Exception:  # noqa: BLE001
                images = []
            for img in images:
                mime = "image/jpeg" if img.name.lower().endswith((".jpg", ".jpeg")) else "image/png"
                try:
                    texts.append(llm.describe_image(img.data, mime, VISION_PROMPT).text)
                    method = "vision"
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s Seite %d: Bildauswertung fehlgeschlagen (%s)", name, i, exc)
            if texts:
                pages.append(Page(i, "\n".join(texts)))
        pages.sort(key=lambda p: p.number or 0)
        if pages:
            return Extracted(pages, method=method)
    if pages:
        return Extracted(pages, message=f"{len(empty_pages)} Seite(n) ohne Textebene")
    return Extracted([], status="skipped",
                     message="Scan ohne Textebene – Bildauswertung ausgeschaltet (LLM_VISION=0)"
                     if not (llm and getattr(llm, "vision", False))
                     else "Scan ohne Textebene – Bildauswertung ergab keinen Text")


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
                return Extracted([], status="skipped",
                                 message="Bild – Bildauswertung ausgeschaltet (LLM_VISION=0)")
            text = llm.describe_image(data, IMAGE_EXT[ext], VISION_PROMPT).text
            result = Extracted([Page(1, text)], method="vision")
    except Exception as exc:  # noqa: BLE001 – one broken file must not stop indexing
        log.warning("Extraktion fehlgeschlagen: %s (%s)", path.name, exc)
        return Extracted([], status="error", message=f"{type(exc).__name__}: {exc}"[:300])
    if result.status == "ok" and not result.text.strip():
        result.status, result.message = "empty", "kein Text gefunden"
    return result
