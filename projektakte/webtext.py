"""Indexed text as people read it (web interface, package "oberflaeche").

Extracted text keeps a little markdown: headings (``# 1 Allgemeines`` – DOCX headings are written
that way as well), pipe tables (``| Angabe | Inhalt |`` with ``|---|---|``, DOCX rows as
``Kennwert | Wert``), bullets and emphasis. The pages never show that syntax:

* :func:`parse_blocks` / :func:`render_html` – structured view of a chunk for the document page
  (headings as headings, HTML tables, lists),
* :func:`plain_text` – clean text for snippets (table rows as "Spalte: Wert"),
* :func:`snippet` – a window of the clean text around the words of a query; it starts and ends at
  word boundaries, and of a mail it shows the body only (:func:`mail_body`: no Betreff/Von/Datum
  lines, salutation or closing – the label of the hit names subject, sender and date),
* :func:`highlight` – ``<mark>`` for the query words. Identifiers (project numbers, file numbers,
  "T90", "DIN 4102") are matched as a whole, never as single parts, and never inside dates; a code
  typed with spaces ("BS 2024 0233") counts as the written code (:func:`join_spaced_codes`),
* :func:`trim_overlap` – the paragraph that chunking repeats at the start of the next section is
  shown only once on the document page.

:class:`QueryMatcher` is the one place that decides what counts as a match; the search page also
uses it to keep hits that literally contain an identifier of the query.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from markupsafe import Markup, escape

from .textutil import QUESTION_WORDS, STOPWORDS, fold, query_terms, stem

SNIPPET_CHARS = 400
ELLIPSIS = "…"

# ------------------------------------------------------------------------------------- blocks
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")
_RULE = re.compile(r"^([-*_])(?:\s*\1){2,}$")
_BULLET = re.compile(r"^[-*+•]\s+(.+)$")
_QUOTE = re.compile(r"^>\s?(.*)$")
_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
_HEADER_CELL_MAX = 30


@dataclass
class Block:
    kind: str  # heading | table | list | para
    text: str = ""
    level: int = 0
    header: list[str] | None = None
    rows: list[list[str]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)


def _cells(line: str) -> list[str] | None:
    """Cells of a table row: markdown ('| a | b |') or as written for DOCX tables ('a | b')."""
    s = line.strip()
    if not (s.startswith("|") or " | " in s):
        return None
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]
    return cells if len(cells) >= 2 else None


def _is_separator(cells: list[str]) -> bool:
    return any(cells) and all(_SEPARATOR_CELL.match(c.replace(" ", "")) for c in cells if c)


def _label_like(cells: list[str]) -> bool:
    """A row that reads like column titles: short cells without digits ('Kennwert | Wert')."""
    return all(c and len(c) <= _HEADER_CELL_MAX and not any(ch.isdigit() for ch in c) for c in cells)


def _table(lines: list[str], i: int, first_block: bool) -> tuple[Block, int]:
    rows: list[list[str]] = []
    header: list[str] | None = None
    while i < len(lines):
        cells = _cells(lines[i])
        if cells is None:
            break
        i += 1
        if _is_separator(cells):
            if len(rows) == 1 and header is None:
                header = rows.pop()
            continue
        rows.append(cells)
    # DOCX tables have no separator row: the first row is a header if it reads like one. A table at
    # the very start of a chunk usually continues the previous chunk (overlap) – no header there.
    if header is None and not first_block and len(rows) >= 2 and _label_like(rows[0]):
        header = rows.pop(0)
    width = max([len(r) for r in rows] + [len(header or [])])
    rows = [r + [""] * (width - len(r)) for r in rows]
    if header is not None:
        header = header + [""] * (width - len(header))
    return Block("table", header=header, rows=rows), i


def parse_blocks(text: str) -> list[Block]:
    """Headings, tables, lists and paragraphs (one per line – chunk lines are paragraphs already)."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line or _RULE.match(line):
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            blocks.append(Block("heading", text=m.group(2).strip(), level=len(m.group(1))))
            i += 1
            continue
        cells = _cells(line)
        if cells is not None:
            if not line.startswith("|") and (i + 1 >= len(lines) or _cells(lines[i + 1]) is None):
                # a single 'a | b' line is text with separators (signature, overlap), no table
                blocks.append(Block("para", text=" · ".join(c for c in cells if c)))
                i += 1
                continue
            block, i = _table(lines, i, first_block=not blocks)
            blocks.append(block)
            continue
        m = _BULLET.match(line)
        if m:
            items = []
            while i < len(lines) and (b := _BULLET.match(lines[i].strip())):
                items.append(b.group(1).strip())
                i += 1
            blocks.append(Block("list", items=items))
            continue
        q = _QUOTE.match(line)
        blocks.append(Block("para", text=(q.group(1) if q else line).strip()))
        i += 1
    return [b for b in blocks if b.kind != "para" or b.text]


# inline markdown: images, links, emphasis, code, escapes
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)\s]+(?:\s+\"[^\"]*\")?\)")
_BOLD = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])"
                     r"|(?<![\w_])_(?=[^\s_])([^_\n]+?)(?<=[^\s_])_(?![\w_])")
_CODE = re.compile(r"`([^`\n]+)`")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>])")


def inline_plain(text: str) -> str:
    """Inline markdown removed: '**Auflage 4**' → 'Auflage 4', '[Plan](plan.pdf)' → 'Plan'."""
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _CODE.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    return _ESCAPE.sub(r"\1", text)


def _inline_html(text: str) -> Markup:
    """Escaped text with bold/italic/code as HTML; links and images become their text (no URLs)."""
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    parts: list[str] = []
    pos = 0
    for m in _CODE.finditer(text):
        parts.append(_emphasis(text[pos:m.start()]))
        parts.append(f"<code>{escape(m.group(1))}</code>")
        pos = m.end()
    parts.append(_emphasis(text[pos:]))
    return Markup("".join(parts))


def _emphasis(text: str) -> str:
    html = str(escape(text))  # escaping leaves * and _ alone
    html = _BOLD.sub(r"<strong>\2</strong>", html)
    html = _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", html)
    return _ESCAPE.sub(r"\1", html)


def _row_text(cells: list[str], header: list[str] | None) -> str:
    cells = [inline_plain(c) for c in cells]
    if len(cells) == 2 and cells[0] and cells[1]:
        if cells[0].endswith(":"):
            return f"{cells[0]} {cells[1]}"
        return f"{cells[0]}: {cells[1]}" if ":" not in cells[0] else f"{cells[0]} · {cells[1]}"
    if header:
        pairs = [f"{inline_plain(h)}: {c}" if h else c for h, c in zip(header, cells) if c]
        return " · ".join(pairs)
    return " · ".join(c for c in cells if c)


def plain_text(text: str) -> str:
    """Clean text without markdown syntax, one line per paragraph, heading or table row."""
    out: list[str] = []
    for b in parse_blocks(text):
        if b.kind == "heading":
            out.append(inline_plain(b.text))
        elif b.kind == "table":
            if b.header and len(b.header) > 2 and not b.rows:
                out.append(" · ".join(inline_plain(h) for h in b.header if h))
            out.extend(t for t in (_row_text(r, b.header) for r in b.rows) if t)
        elif b.kind == "list":
            out.extend(f"• {inline_plain(item)}" for item in b.items)
        else:
            out.append(inline_plain(b.text))
    return "\n".join(out)


def render_html(blocks: list[Block], heading_base: int = 3) -> Markup:
    """Safe HTML for parsed blocks; markdown heading level 1 becomes <h{heading_base}>."""
    html: list[str] = []
    for b in blocks:
        if b.kind == "heading":
            level = min(6, heading_base + b.level - 1)
            html.append(f"<h{level}>{_inline_html(b.text)}</h{level}>")
        elif b.kind == "table":
            html.append('<div class="table-wrap"><table class="doc-table">')
            if b.header:
                html.append("<thead><tr>" + "".join(f"<th>{_inline_html(c)}</th>" for c in b.header)
                            + "</tr></thead>")
            html.append("<tbody>")
            for r in b.rows:
                html.append("<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r) + "</tr>")
            html.append("</tbody></table></div>")
        elif b.kind == "list":
            html.append("<ul>" + "".join(f"<li>{_inline_html(i)}</li>" for i in b.items) + "</ul>")
        else:
            html.append(f"<p>{_inline_html(b.text)}</p>")
    return Markup("\n".join(html))


def same_heading(a: str | None, b: str | None) -> bool:
    norm = lambda s: " ".join(fold(inline_plain(s or "")).split())  # noqa: E731
    return bool(a and b) and norm(a) == norm(b)


_PATH_BREAK = re.compile(r"(?<=[/\\_])")


def breakable_path(text: str | None) -> Markup:
    """A path or file name that may wrap after '/', '\\' and '_' (<wbr>) – on small screens it breaks
    between folder names instead of in the middle of a word."""
    parts = _PATH_BREAK.split(text or "")
    return Markup("<wbr>").join(escape(part) for part in parts)


# ------------------------------------------------------------------------------------- matching
# A word with its joined parts: "BS-2024-0137", "BA-412/2023", "09.01.2024", "A2.2", "Rauch".
_COMPOUND = re.compile(r"[^\W_]+(?:[-/.][^\W_]+)*")
_SEP = re.compile(r"[-/.]")
_DOTTED_NUMBER = re.compile(r"^\d+(?:\.\d+)+$")  # dates, decimals, thousands, section numbers
_ISO_DATE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}$")
MIN_IDENTIFIER_CHARS = 3  # "T90", "F30", "0137" – a lone "1" is no identifier
# A reference code typed with spaces: "BS 2024 0233", "bs 2023 0412", "BS2024 0233" – letters, then two
# to four groups of digits (the shape of textutil's reference codes). A single group stays apart
# ("DIN 4102"), lower-case letters count only as a short code ("bs 2023 0412", not "seite 12 13").
_SPACED_CODE = re.compile(r"(?<![^\W_])([^\W\d_]{1,6})([-/]?|\s?)(\d{2,6}(?:(?:\s+|[-/])\d{2,6}){1,3})(?![^\W_])")
_SPACES = re.compile(r"\s+")


def join_spaced_codes(text: str) -> str:
    """Reference codes typed with spaces in their written form: 'BS 2024 0233' → 'BS-2024-0233'.
    Then they match the code in a text as a whole (not '2024' alone), name the project like
    'BS-2024-0233' does, and their letters are not taken for an abbreviation (BS → Brandschutz)."""
    def join(m: re.Match) -> str:
        prefix, sep, digits = m.groups()
        short_code = len(prefix) <= 3 and fold(prefix) not in STOPWORDS and fold(prefix) not in QUESTION_WORDS
        if not any(c.isspace() for c in m.group(0)) or not (prefix.isupper() or short_code):
            return m.group(0)
        return f"{prefix}{'/' if sep == '/' else '-'}{_SPACES.sub('-', digits)}"
    return _SPACED_CODE.sub(join, text or "")


def _segments(token: str) -> list[tuple[int, int, str]]:
    """(start, end, folded text) of the parts of a compound token."""
    out, pos = [], 0
    for part in _SEP.split(token):
        out.append((pos, pos + len(part), fold(part)))
        pos += len(part) + 1
    return out


def _whole_only(token: str) -> bool:
    """Tokens that must not be matched in parts: dates and plain numbers with dots."""
    return bool(_DOTTED_NUMBER.match(token) or _ISO_DATE.match(token))


class QueryMatcher:
    """What a search query matches in a text – shared by highlighting, snippets and the search page.

    * identifiers (query tokens with a digit) match whole tokens or whole runs of parts of a
      longer identifier ("2024-0137" in "BS-2024-0137"), never parts of dates or decimal numbers;
    * words match like the search does: case and accents ignored, word forms, compounds by the
      beginning (Rauchableitung → Rauchableitungsöffnung) or the head (Genehmigung →
      Baugenehmigung, Baubesprechung → Besprechung) – not by the first part only (Löschwasserteich
      does not mark Löschwasser) – and single-word abbreviations/synonyms (BMA → Brandmeldeanlage).
    """

    def __init__(self, query: str):
        query = join_spaced_codes(query or "")  # "BS 2024 0233" is the code BS-2024-0233
        self.identifiers: list[tuple[str, ...]] = []
        rest: list[str] = []
        for m in _COMPOUND.finditer(query):
            token = m.group(0)
            if any(c.isdigit() for c in token):
                segs = tuple(s for _, _, s in _segments(token) if s)
                if segs and segs not in self.identifiers:
                    self.identifiers.append(segs)
            else:
                rest.append(token)
        self.terms: set[str] = set()  # stems and folded words of the query
        self.alts: set[str] = set()  # single-word synonyms and word forms
        for qt in query_terms(" ".join(rest)):
            if qt.word in STOPWORDS or qt.word in QUESTION_WORDS:
                continue
            for t in {qt.term, qt.word}:
                if len(t) >= 3:
                    self.terms.add(t)
            for alt in qt.alts:
                if len(alt) == 1 and len(alt[0]) >= 3:
                    self.alts.add(alt[0])

    def __bool__(self) -> bool:
        return bool(self.identifiers or self.terms or self.alts)

    @property
    def strong_identifiers(self) -> list[tuple[str, ...]]:
        """Identifiers specific enough that a hit containing one literally always counts."""
        return [i for i in self.identifiers if sum(len(s) for s in i) >= MIN_IDENTIFIER_CHARS]

    def word_matches(self, word: str) -> bool:
        w = fold(word)
        if len(w) < 2 or w.isdigit():
            return False
        sw = stem(w)
        for t in self.terms:
            if sw == t or w == t:
                return True
            if len(t) >= 4 and w.startswith(t):  # word forms, compounds starting with the word
                return True
            if len(t) >= 5 and w.endswith(t):  # compound head: Genehmigung → Baugenehmigung
                return True
            if len(sw) >= 6 and len(t) - len(sw) >= 3 and t.endswith(sw):  # the text names the head:
                return True  # Baubesprechung → Besprechung (not short heads such as Anlage, Raum)
        return any(sw == a or w == a or (len(a) >= 4 and w.startswith(a)) for a in self.alts)

    def _identifier_span(self, token: str, idents: list[tuple[str, ...]]) -> tuple[int, int] | None:
        segs = _segments(token)
        folded = tuple(s for _, _, s in segs)
        whole_only = _whole_only(token)
        best: tuple[int, int] | None = None
        for ident in idents:
            n = len(ident)
            if folded == ident:
                return 0, len(token)
            if whole_only or n > len(folded):
                continue
            for i in range(len(folded) - n + 1):
                if folded[i:i + n] == ident:
                    span = (segs[i][0], segs[i + n - 1][1])
                    if best is None or span[1] - span[0] > best[1] - best[0]:
                        best = span
                    break
        return best

    def spans(self, text: str) -> list[tuple[int, int, str]]:
        """Matches in ``text`` as (start, end, key) – key is the folded matched text."""
        out: list[tuple[int, int, str]] = []
        if not self:
            return out
        for m in _COMPOUND.finditer(text or ""):
            token, base = m.group(0), m.start()
            if self.identifiers and any(c.isdigit() for c in token):
                span = self._identifier_span(token, self.identifiers)
                if span:
                    out.append((base + span[0], base + span[1], fold(token[span[0]:span[1]])))
                    continue
            for start, end, part in _segments(token):
                if part and not any(c.isdigit() for c in part) and self.word_matches(token[start:end]):
                    out.append((base + start, base + end, stem(part)))
        return out

    def has_identifier(self, *texts: str | None) -> bool:
        """Does one of the texts literally contain a (specific) identifier of the query?"""
        idents = self.strong_identifiers
        if not idents:
            return False
        for text in texts:
            for m in _COMPOUND.finditer(text or ""):
                token = m.group(0)
                if any(c.isdigit() for c in token) and self._identifier_span(token, idents):
                    return True
        return False


@lru_cache(maxsize=256)
def matcher(query: str) -> QueryMatcher:
    return QueryMatcher(query)


# ------------------------------------------------------------------------------------- output
def highlight(text: str, query: str) -> Markup:
    """Escape ``text`` and mark the words of the search query (<mark>)."""
    text = text or ""
    spans = matcher(query or "").spans(text)
    if not spans:
        return Markup(escape(text))
    out, pos = [], 0
    for start, end, _ in spans:
        out.append(escape(text[pos:start]))
        out.append(Markup("<mark>") + escape(text[start:end]) + Markup("</mark>"))
        pos = end
    out.append(escape(text[pos:]))
    return Markup("").join(out)


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ\"„])")
_SPACE = re.compile(r"\s")
# A window may grow by width // LINE_SLACK characters to start at its line instead of mid-line, and
# by width // WORD_SLACK to start (or end) at a word instead of inside one.
LINE_SLACK = 10
WORD_SLACK = 4


def _word_starts(plain: str, lo: int, hi: int) -> list[int]:
    """Positions in [lo, hi] where a word begins (start of the text or after white space)."""
    return [i for i in range(max(0, lo), min(hi, len(plain)) + 1) if i == 0 or plain[i - 1].isspace()]


def _line_start(plain: str, pos: int) -> int:
    return max(plain.rfind("\n", 0, pos), plain.rfind("\r", 0, pos)) + 1


def _window(plain: str, spans: list[tuple[int, int, str]], width: int) -> tuple[int, int, int]:
    """(start, end, end of the last match) of the window of about ``width`` characters with the most
    different query words (the earliest on a tie).

    The window never starts inside a word. It starts at the beginning of the line or sentence of its
    first match when that is not too far back, else at a word with a little context before the
    match. If no word begins in that room (a match inside a long token such as a mail address), it
    starts a bit earlier at its line or word and the window grows instead of cutting the word."""
    anchor, last_end, best_score = 0, 0, (-1, -1)
    for i, (s0, _, _) in enumerate(spans):
        inside = [sp for sp in spans[i:] if sp[1] <= s0 + width]
        score = (len({k for _, _, k in inside}), len(inside))
        if score > best_score:
            anchor, best_score = s0, score
            last_end = max([e for _, e, _ in inside] + [spans[i][1]])
    if last_end <= width * 9 // 10:  # the beginning of the text shows the matches as well
        return 0, width, last_end
    room = max(0, width - (last_end - anchor))  # context that still fits before the first match
    lead = min(room, width * 2 // 3)
    start = line = _line_start(plain, anchor)
    if anchor - line > lead:
        sentences = [m.end() for m in _SENTENCE.finditer(plain, line, anchor)]
        start = next((b for b in sentences if anchor - b <= lead), line)
    # neither the sentence nor – at the cost of a few characters more – the line: a word before the match
    if anchor - start > lead and anchor - line > lead + width // LINE_SLACK:
        back = min(lead, width // 6)
        words = _word_starts(plain, max(line, anchor - lead), anchor)
        with_context = [w for w in words if w <= anchor - back]
        if with_context:
            start = with_context[-1]
        elif words:
            start = words[0]
        elif anchor - line <= lead + width // WORD_SLACK:
            start = line
        else:  # the word of the match begins further back: start at it if it is not too long ago
            before = _word_starts(plain, anchor - lead - width // WORD_SLACK, anchor)
            start = before[-1] if before else anchor
    if len(plain) - start < width:  # close to the end: use the full width, from a line or word if possible
        target = max(0, len(plain) - width)
        if target == 0:
            start = 0
        else:
            line_at = plain.find("\n", target, start)
            words = _word_starts(plain, target, start)
            start = line_at + 1 if line_at >= 0 else words[0] if words else start
    return start, min(len(plain), max(start + width, last_end)), last_end


def _end_at_boundary(plain: str, start: int, end: int, floor: int, width: int) -> int:
    """End of a snippet at a sentence, line or word boundary – never before ``floor`` (the end of
    the last match) and never inside a word if a boundary follows soon."""
    if end >= len(plain):
        return len(plain)
    cut = max(plain.rfind(". ", start, end) + 1, plain.rfind("\n", start, end))
    if cut < max(floor, start + width // 2):
        cut = max((m.start() for m in _SPACE.finditer(plain, start, end)), default=-1)
    if cut >= floor:
        return cut
    if end > start and (plain[end].isspace() or plain[end - 1].isspace()):
        return end
    nxt = _SPACE.search(plain, end)
    stop = nxt.start() if nxt else len(plain)
    return stop if stop - end <= width // WORD_SLACK else end


# Header lines of an indexed mail section (mails.store.mail_text, extract.py for .eml/.msg files) –
# the label of the hit shows subject, sender and date already – plus salutation and closing.
_MAIL_HEADER = re.compile(r"^(?:Betreff|Von|An|Cc|Datum|Gesendet):(?:[ \t]|$)")
_GREETING = re.compile(r"^(?:Guten\s+(?:Tag|Morgen|Abend)|Hallo|Moin|Servus|Sehr\s+geehrte[rs]?|Liebe[rs]?|Hi)\b"
                       r"(?:[^,;:.!?\n]|\b(?:Dr|Prof|Dipl|Ing|Hr|Fr)\.){0,80}(?:[,!]\s*|$)", re.I)
_CLOSING = re.compile(r"^(?:Mit\s+(?:freundlichen|besten|herzlichen|kollegialen|freundlichem)\s+Grü(?:ß|ss)en?"
                      r"|(?:Freundliche|Viele|Beste|Herzliche|Schöne|Liebe|Sonnige)\s+Grü(?:ß|ss)e"
                      r"|Gru(?:ß|ss)|Grü(?:ß|ss)e|MfG|Best regards|Kind regards)\b", re.I)
_CLOSING_LINE_MAX = 60


def mail_body(plain: str) -> str:
    """The text of a mail section without what the hit's label repeats or what says nothing: header
    lines (Betreff, Von, Datum), the salutation and the closing with the signature. Text that does
    not start like an indexed mail (``Betreff:`` followed by ``Von:``/``Datum:``) is returned as
    it is, and so is a mail whose body would be empty."""
    lines = plain.split("\n")
    if not lines or not lines[0].startswith("Betreff:"):
        return plain
    i = 0
    while i < len(lines) and _MAIL_HEADER.match(lines[i].strip()):
        i += 1
    if not any(ln.startswith(("Von:", "Datum:")) for ln in lines[1:i]):
        return plain
    body = lines[i:]
    while body and not body[0].strip():
        body.pop(0)
    if body and (g := _GREETING.match(body[0].strip())):
        rest = body[0].strip()[g.end():].strip()
        body = ([rest] if rest else []) + body[1:]
    for j, ln in enumerate(body):
        if j > 0 and len(ln.strip()) <= _CLOSING_LINE_MAX and _CLOSING.match(ln.strip()):
            body = body[:j]
            break
    text = "\n".join(body).strip()
    return text or plain


def snippet(text: str, query: str = "", width: int = SNIPPET_CHARS) -> str:
    """Clean text of a chunk cut to about ``width`` characters around the words of ``query`` –
    the passage that matches, not the beginning of the chunk. '…' marks cut text; it never starts
    or ends inside a word. Of a mail only the body counts (no header lines, greeting or closing):
    the hit's label shows subject, sender and date."""
    plain = mail_body(plain_text(text))
    if len(plain) <= width:
        return plain
    spans = matcher(query or "").spans(plain)
    start, end, last_end = _window(plain, spans, width) if spans else (0, width, 0)
    if end < len(plain):  # end at a sentence or word boundary, after the last match
        end = _end_at_boundary(plain, start, end, max(last_end, start + width // 3), width)
    body = plain[start:end].strip()
    return (ELLIPSIS + " " if start > 0 else "") + body + (" " + ELLIPSIS if end < len(plain) else "")


# ------------------------------------------------------------------------------------- overlap
MAX_OVERLAP_LINES = 8


def _norm_line(line: str) -> str:
    return " ".join(line.split())


def trim_overlap(text: str, following: str) -> str:
    """``text`` without the lines at its end that the following section repeats at its beginning
    word for word (chunking.py carries the last paragraph of a section over into the next one).
    At least one line of ``text`` always stays; blank lines do not count."""
    lines = text.split("\n")
    filled = [i for i, ln in enumerate(lines) if ln.strip()]
    nxt = [_norm_line(ln) for ln in following.split("\n") if ln.strip()]
    for k in range(min(len(filled) - 1, len(nxt), MAX_OVERLAP_LINES), 0, -1):
        if [_norm_line(lines[i]) for i in filled[-k:]] == nxt[:k]:
            return "\n".join(lines[:filled[-k]]).rstrip()
    return text


def passage(snippet_text: str | None, full_text: str | None, query: str = "",
            width: int = SNIPPET_CHARS) -> str:
    """Text shown for a source of an answer: the passage the answer pipeline chose (made clean,
    '…' where it was cut out of the chunk) or, without one, the snippet around the question."""
    raw = (snippet_text or "").strip()
    if raw and query and full_text and len(full_text) > len(raw) + 20 and full_text.startswith(raw):
        # only the beginning of the chunk (older results): if the question's words are not in it but
        # further down, show the passage around them instead
        m = matcher(query)
        if not m.spans(plain_text(raw)) and m.spans(plain_text(full_text)):
            return snippet(full_text, query, width)
    core = plain_text(raw).strip()
    lead = core.startswith((ELLIPSIS, "..."))
    trail = core.endswith((ELLIPSIS, "..."))
    if lead:
        core = core[1:] if core.startswith(ELLIPSIS) else core[3:]
    if trail:
        core = core[:-1] if core.endswith(ELLIPSIS) else core[:-3]
    core = core.strip()
    if not core:
        return snippet(full_text or "", query, width)
    flat_full = " ".join(plain_text(full_text or "").split())
    flat_core = " ".join(core.split())
    at = flat_full.find(flat_core) if flat_full else -1
    if at >= 0:  # where the passage sits in the chunk decides about the '…'
        lead, trail = at > 0, at + len(flat_core) < len(flat_full)
    return (ELLIPSIS + " " if lead else "") + core + (" " + ELLIPSIS if trail else "")
