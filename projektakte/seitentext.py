"""The raw text of the page model (Nemotron Parse) → the text of a page as it is indexed.

The model answers a text page with blocks, each ``<x_left><y_top>content<x_right><y_bottom><class_Kind>`` with
coordinates relative to the page (0–1) and a class (Text, Title, Section-header, Table, Page-header, Page-footer,
Picture, Caption, List-item, …); the content is Markdown already. Here the marks are removed and the blocks kept in
the order given (the model's reading order). Tables stay as the model wrote them.

The model sees one page at a time and writes the letterhead and the footer of every page. A header or footer that
comes back on the pages of a document is kept once (:func:`document_texts`).

Pure functions: the raw text is stored (``page_types.raw``), so these rules can change without the model reading a
page again.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_BLOCK = re.compile(r"<x_([\d.]+)><y_([\d.]+)>(.*?)<x_([\d.]+)><y_([\d.]+)><class_([^>]+)>", re.DOTALL)
_MARK = re.compile(r"<(?:[xy]_[\d.]+|class_[^>]+)>")
_ESCAPED = re.compile(r"\\([<>])")
_DIGITS = re.compile(r"\d+")
_SPACE = re.compile(r"\s+")
# what the service writes where it cut a repetition loop of the model short
LOOP_NOTE = re.compile(r"\[Schleife des Modells:[^\]]*\]")
REPEATING = ("Page-header", "Page-footer")
MIN_PAGES = 3  # a header or footer on fewer pages is not "on every page"


@dataclass(frozen=True)
class Block:
    klasse: str
    text: str
    box: tuple[float, float, float, float] | None  # left, top, right, bottom (0–1); None: text outside a block


def _number(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _loose(text: str) -> list[Block]:
    """Text outside a block (an answer cut short, or a model that left the format): kept, without stray marks."""
    text = _MARK.sub("", text).strip()
    return [Block("Text", text, None)] if text else []


def parse_blocks(raw: str) -> list[Block]:
    """The blocks of a page in the order the model gave them. Nothing is lost: text outside blocks comes as a
    block of the class Text without coordinates."""
    blocks: list[Block] = []
    end = 0
    for m in _BLOCK.finditer(raw or ""):
        blocks += _loose(raw[end:m.start()])
        box = (_number(m.group(1)), _number(m.group(2)), _number(m.group(4)), _number(m.group(5)))
        blocks.append(Block(m.group(6).strip(), m.group(3).strip(), box))
        end = m.end()
    blocks += _loose((raw or "")[end:])
    return blocks


def block_key(block: Block) -> str:
    """What makes two headers or footers "the same": lower case, every number a ``#`` (page numbers, dates),
    white space as one blank."""
    return _SPACE.sub(" ", _DIGITS.sub("#", block.text.lower())).strip()


def repeated_keys(pages: list[list[Block]]) -> set[str]:
    """Keys of the headers and footers on at least MIN_PAGES pages and at least half of the pages."""
    counts: dict[str, int] = {}
    for blocks in pages:
        for key in {block_key(b) for b in blocks if b.klasse in REPEATING}:
            if key:
                counts[key] = counts.get(key, 0) + 1
    return {key for key, n in counts.items() if n >= MIN_PAGES and n * 2 >= len(pages)}


def _clean(text: str) -> str:
    text = LOOP_NOTE.sub("", text)
    return _ESCAPED.sub(r"\1", text).strip()


def page_text(blocks: list[Block], drop: frozenset[str] | set[str] = frozenset()) -> str:
    """The text of a page: its blocks without marks, a blank line between them. ``drop``: keys of headers and
    footers left out on this page."""
    parts = []
    for b in blocks:
        if b.klasse in REPEATING and block_key(b) in drop:
            continue
        text = b.text if b.klasse == "Table" else _clean(b.text)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def document_texts(raws: dict[int, str]) -> dict[int, str]:
    """Page number → text for the pages of one document the model wrote out (``raws``: their raw texts). A header
    or footer that comes back (:func:`repeated_keys`) stays on the first page that has it only."""
    numbers = sorted(raws)
    parsed = {n: parse_blocks(raws[n]) for n in numbers}
    repeated = repeated_keys([parsed[n] for n in numbers])
    seen: set[str] = set()
    texts = {}
    for n in numbers:
        here = {block_key(b) for b in parsed[n] if b.klasse in REPEATING} & repeated
        texts[n] = page_text(parsed[n], here & seen)
        seen |= here
    return texts
