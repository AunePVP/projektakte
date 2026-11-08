"""Split extracted text into overlapping sections (chunks) that keep their page number."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .extract import Page

MAX_CHARS = 900
OVERLAP_CHARS = 250
_LIST_START = re.compile(r"^(\s*(#|[-•*]|\d+[.)]|TOP\s*\d|Auflage\s+\d+|[A-ZÄÖÜ][^\s]{0,30}:)\s*)")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$")


@dataclass
class Chunk:
    ord: int
    page: int | None
    heading: str | None
    text: str


def _units(text: str) -> list[str]:
    """Paragraph-like units: soft line breaks (PDF wrapping) are joined, list items kept apart."""
    units: list[str] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        current = ""
        for ln in lines:
            if not current:
                current = ln
            elif (_LIST_START.match(ln) or _HEADING.match(current)
                  or current.endswith((".", ":", "!", "?", "|")) or len(current) < 40):
                units.append(current)
                current = ln
            else:
                current += " " + ln
        if current:
            units.append(current)
    return units


def _split_long(unit: str, max_chars: int) -> list[str]:
    if len(unit) <= max_chars:
        return [unit]
    sentences = re.split(r"(?<=[.!?;])\s+", unit)
    out, cur = [], ""
    for s in sentences:
        while len(s) > max_chars:  # very long sentence without punctuation
            cut = s.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if cur:
                out.append(cur)
                cur = ""
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if cur and len(cur) + 1 + len(s) > max_chars:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def chunk_pages(pages: list[Page], max_chars: int = MAX_CHARS,
                overlap_chars: int = OVERLAP_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading: str | None = None
    for page in pages:
        units: list[str] = []
        for u in _units(page.text):
            units.extend(_split_long(u, max_chars))
        buf: list[str] = []
        buf_heading = heading

        def flush():
            if buf and any(not _HEADING.match(b) for b in buf):
                chunks.append(Chunk(len(chunks), page.number, buf_heading, "\n".join(buf)))

        for u in units:
            m = _HEADING.match(u)
            if m:
                # a new heading starts a new chunk
                flush()
                heading = m.group(1).strip()
                buf, buf_heading = [u], heading
                continue
            if buf and sum(len(b) + 1 for b in buf) + len(u) > max_chars:
                flush()
                last = buf[-1]
                buf = [last] if len(last) <= overlap_chars and not _HEADING.match(last) else []
                if buf and sum(len(b) + 1 for b in buf) + len(u) > max_chars:
                    buf = []
                buf_heading = heading
            buf.append(u)
        flush()
    return chunks
