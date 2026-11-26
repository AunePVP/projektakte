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
    return texts_of_blocks({n: parse_blocks(raw) for n, raw in raws.items()})


def texts_of_blocks(parsed: dict[int, list[Block]]) -> dict[int, str]:
    """Page number → text for pages given as blocks (the model's, or the text layer in the model's blocks), with
    the rule for headers and footers that come back."""
    numbers = sorted(parsed)
    repeated = repeated_keys([parsed[n] for n in numbers])
    seen: set[str] = set()
    texts = {}
    for n in numbers:
        here = {block_key(b) for b in parsed[n] if b.klasse in REPEATING} & repeated
        texts[n] = page_text(parsed[n], here & seen)
        seen |= here
    return texts


# -- pages with a real text layer -----------------------------------------------------------------------------
# The text layer has the exact characters; the model, which sees the page, has its structure: paragraphs, headings,
# list items, tables, headers and footers, columns in reading order. Here the lines of the text layer (with their
# place on the page, pdfseiten.Segment as [x0, y0, x1, y1, text]) are put into the model's blocks. Nothing of the
# layer is ever left out: a line in no block stays as a line of its own. What only the image shows (a stamp, a
# scanned insert, text drawn as outlines) comes from the model.
MIN_COVERAGE = 0.5  # the text layer has less than this share of the words the model read: it is not what the page shows
MIN_WORDS = 8  # words the model must have read before the two texts are compared
_HEADING_MARK = re.compile(r"(#{1,6})\s")
_WORD = re.compile(r"[^\W_]{2,}")
_MARKDOWN = re.compile(r"[*#|`>]+")
_BOX_SLACK = 0.004  # a line's centre may lie this far outside a block's box (boxes are drawn tightly)


def join_lines(lines: list[str]) -> str:
    """The lines of a paragraph as running text. A line ending with a hyphen after a letter takes the next line
    without a blank ("Brandschutz-" + "Dienststelle"); words divided at the line end were joined by the reader."""
    out = ""
    for line in (ln.strip() for ln in lines):
        if not line:
            continue
        if not out:
            out = line
        elif out.endswith("-") and len(out) > 1 and out[-2].isalpha() and line[0].isalpha():
            out += line
        else:
            out += " " + line
    return out


def _words(text: str) -> set[str]:
    from .textutil import fold

    return set(_WORD.findall(fold(_MARKDOWN.sub(" ", text or ""))))


def coverage(model_text: str, layer_text: str) -> float | None:
    """Share of the (different) words the model read that the text layer has too – spelling of umlauts and ß
    aside. None if the model read fewer than MIN_WORDS words: too few to tell."""
    model = _words(model_text)
    if len(model) < MIN_WORDS:
        return None
    return len(model & _words(layer_text)) / len(model)


def _rows(segments: list) -> list[list]:
    """Pieces of the same line (columns of a row) together, in the order given."""
    rows: list[list] = []
    for seg in segments:
        last = rows[-1][-1] if rows else None
        if last is not None:
            mid, last_mid = (seg[1] + seg[3]) / 2, (last[1] + last[3]) / 2
            if abs(mid - last_mid) < min(seg[3] - seg[1], last[3] - last[1]) / 2 and seg[0] >= last[0]:
                rows[-1].append(seg)
                continue
        rows.append([seg])
    return rows


def _block_of(seg, boxes: list[tuple[int, tuple]]) -> int | None:
    cx, cy = (seg[0] + seg[2]) / 2, (seg[1] + seg[3]) / 2
    best, best_area = None, None
    for index, (x0, y0, x1, y1) in boxes:
        if x0 - _BOX_SLACK <= cx <= x1 + _BOX_SLACK and y0 - _BOX_SLACK <= cy <= y1 + _BOX_SLACK:
            area = (x1 - x0) * (y1 - y0)
            if best_area is None or area < best_area:
                best, best_area = index, area
    return best


def _block_text(klasse: str, segments: list) -> str:
    rows = _rows(segments)
    if klasse == "Table":  # rows stay rows, their cells apart
        return "\n".join(" | ".join(s[4].strip() for s in row) for row in rows)
    return join_lines([" ".join(s[4].strip() for s in row) for row in rows])


def layer_blocks(segments: list, model_blocks: list[Block]) -> list[Block]:
    """The lines of the text layer in the model's blocks, in the model's reading order: a block's text is the
    exact text of the lines lying in it (with the model's heading mark). Lines in no block stay, as lines, after
    the block before them. A block without lines keeps the model's text if the layer does not have it (only the
    image shows it); if the layer has it elsewhere, the block is left out (no text twice)."""
    boxes = [(i, b.box) for i, b in enumerate(model_blocks) if b.box is not None]
    inside: dict[int, list] = {}
    loose_after: dict[int | None, list[list]] = {}  # block index (None: before all) → groups of lines in no block
    anchor: int | None = None
    group: list | None = None
    for seg in segments:
        if not str(seg[4]).strip():
            continue
        index = _block_of(seg, boxes)
        if index is None:
            if group is None:
                group = []
                loose_after.setdefault(anchor, []).append(group)
            group.append(seg)
        else:
            inside.setdefault(index, []).append(seg)
            anchor, group = index, None
    layer_words = _words(" ".join(str(s[4]) for s in segments))

    def loose(key) -> list[Block]:
        return [Block("Text", "\n".join(" ".join(s[4].strip() for s in row) for row in _rows(g)), None)
                for g in loose_after.get(key, [])]

    out = loose(None)
    for i, block in enumerate(model_blocks):
        if i in inside:
            text = _block_text(block.klasse, inside[i])
            mark = _HEADING_MARK.match(block.text)
            if mark and not text.startswith("#"):
                text = f"{mark.group(1)} {text}"
            out.append(Block(block.klasse, text, block.box))
        elif block.text.strip() and block.klasse != "Picture":
            words = _words(block.text)
            if words and len(words & layer_words) / len(words) < MIN_COVERAGE:
                out.append(block)  # only the image shows this text
        out += loose(i)
    return out


def compose_layer(segments: list, raw: str | None, *, schleife: bool = False,
                  finish: str | None = None) -> tuple[list[Block], float | None, str]:
    """The blocks of a page with a real text layer, how much of what the model read the layer has (coverage,
    None if not comparable) and where the text is from: "ebene" – the layer's text, in the model's blocks if
    there are any – or "modell": the layer has less than MIN_COVERAGE of the words the model read on the page
    (a font without a usable character map, hidden text, a page that is mostly an image), so the page's text is
    what the model saw. A model answer that is not whole (loop cut short, cut off) never replaces the layer."""
    model_blocks = [b for b in parse_blocks(raw or "") if b.text.strip() or b.box is not None]
    if not any(b.text.strip() for b in model_blocks):
        rows = [" ".join(s[4].strip() for s in row) for row in _rows([s for s in segments if str(s[4]).strip()])]
        return ([Block("Text", "\n".join(rows), None)] if rows else []), None, "ebene"
    share = coverage(page_text(model_blocks), " ".join(str(s[4]) for s in segments))
    whole = not schleife and finish != "length"
    if share is not None and share < MIN_COVERAGE and whole:
        return model_blocks, share, "modell"
    return layer_blocks(segments, model_blocks), share, "ebene"
