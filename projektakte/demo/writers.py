"""Byte-deterministic writers for PDF, DOCX and PNG (standard library only).

The demo data must be reproducible down to the file hash, so no timestamps are embedded.
"""
from __future__ import annotations

import functools
import io
import struct
import textwrap
import zipfile
import zlib
from xml.sax.saxutils import escape

# ----------------------------------------------------------------------------- PDF
_PDF_REPLACE = {"≥": ">=", "≤": "<=", "→": "->", "‑": "-", "…": "...", " ": " "}
PAGE_W, PAGE_H = 595, 842  # A4 in points
_LINES_PER_PAGE = 54
_WRAP = 92


def _pdf_text(s: str) -> bytes:
    for a, b in _PDF_REPLACE.items():
        s = s.replace(a, b)
    raw = s.encode("cp1252", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _layout(paragraphs: list[str]) -> list[list[tuple[str, bool]]]:
    """Wrap paragraphs into lines; a paragraph starting with '# ' is bold. Returns pages."""
    pages: list[list[tuple[str, bool]]] = [[]]
    for para in paragraphs:
        if para == "\f":  # explicit page break
            if pages[-1]:
                pages.append([])
            continue
        bold = para.startswith("# ")
        text = para[2:] if bold else para
        lines = textwrap.wrap(text, _WRAP) or [""]
        if len(pages[-1]) + len(lines) > _LINES_PER_PAGE:
            pages.append([])
        for line in lines:
            pages[-1].append((line, bold))
        pages[-1].append((" ", False))  # blank line between paragraphs (kept by text extraction)
    return [p for p in pages if p]


def pdf_bytes(paragraphs: list[str]) -> bytes:
    """Minimal multi-page PDF with Helvetica (WinAnsi) text that pypdf can extract."""
    pages = _layout(paragraphs)
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    catalog = add(b"")  # placeholder, filled later
    pages_id = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_b = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"
    )
    page_ids = []
    for lines in pages:
        ops = [b"BT", b"14 TL", b"56 790 Td"]
        current = None
        for text, bold in lines:
            f = b"/F2 11 Tf" if bold else b"/F1 10 Tf"
            if f != current:
                ops.append(f)
                current = f
            ops.append(b"(" + _pdf_text(text) + b") Tj T*")
        ops.append(b"ET")
        stream = b"\n".join(ops)
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
                b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
                % (pages_id, PAGE_W, PAGE_H, font, font_b, content)
            )
        )
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objects) + 1, catalog, xref))
    return out.getvalue()


# ----------------------------------------------------------------------------- DOCX
_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
_DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _style(sid: str, name: str, size: int, bold: bool) -> str:
    b = "<w:b/>" if bold else ""
    return (f'<w:style w:type="paragraph" w:styleId="{sid}"><w:name w:val="{name}"/>'
            f'<w:basedOn w:val="Normal"/><w:pPr><w:keepNext/></w:pPr>'
            f'<w:rPr>{b}<w:sz w:val="{size}"/></w:rPr></w:style>')


_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{_W}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
    '<w:rPr><w:sz w:val="22"/></w:rPr></w:style>'
    + _style("Title", "Title", 36, True)
    + _style("Heading1", "heading 1", 28, True)
    + _style("Heading2", "heading 2", 24, True)
    + '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/></w:style>'
    "</w:styles>"
)


def _para(text: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def _table(rows: list[tuple[str, ...]]) -> str:
    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/>'
           "</w:tblPr>"]
    for row in rows:
        out.append("<w:tr>")
        for cell in row:
            out.append(f"<w:tc><w:tcPr><w:tcW w:w=\"4500\" w:type=\"dxa\"/></w:tcPr>{_para(cell)}</w:tc>")
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def docx_bytes(blocks: list[tuple[str, object]]) -> bytes:
    """blocks: ("title"|"h1"|"h2"|"p", text) or ("table", [(cell, cell), …])."""
    styles = {"title": "Title", "h1": "Heading1", "h2": "Heading2", "p": None}
    body = []
    for kind, value in blocks:
        if kind == "table":
            body.append(_table(value))  # type: ignore[arg-type]
        else:
            body.append(_para(str(value), styles[kind]))
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{_W}">'
        f"<w:body>{''.join(body)}<w:sectPr/></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in (
            ("[Content_Types].xml", _CONTENT_TYPES),
            ("_rels/.rels", _RELS),
            ("word/_rels/document.xml.rels", _DOC_RELS),
            ("word/document.xml", document),
            ("word/styles.xml", _STYLES),
        ):
            info = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)
    return buf.getvalue()


# ----------------------------------------------------------------------------- PNG
def png_bytes(width: int = 96, height: int = 64, seed: int = 0) -> bytes:
    """A small grey 'plan' image (lines on white) – stands in for a scanned plan."""
    rows = []
    for y in range(height):
        row = bytearray([0])  # filter type 0
        for x in range(width):
            line = (x % 24 == seed % 24) or (y % 16 == 0) or x in (0, width - 1) or y == height - 1
            row.append(60 if line else 245)
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)  # 8-bit greyscale
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


# ----------------------------------------------------------------------------- escape plan (RGB PNG)
# A small but recognisable "Flucht- und Rettungsplan" in the style of DIN ISO 23601: rescue signs in
# safety green (ISO 7010 E002 emergency exit with direction arrow, E007 assembly point), the fire
# extinguisher sign in red (F001), the escape route as a pale green band with green arrows, an own
# "you are here" symbol (blue ring, so red only ever means fire-fighting equipment) and a legend.
# Only integer arithmetic is used, so the pixels are identical on every platform; every label,
# sign and arrow is recorded with its bounding box (see ``plan_layout``) so tests can check that
# nothing overlaps.

_FONT = {  # 5×7 bitmap font, rows top to bottom; umlauts have two extra rows (dots) above the cell
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    "Ä": "01010 00000 01110 10001 10001 11111 10001 10001 10001",
    "Ö": "01010 00000 01110 10001 10001 10001 10001 10001 01110",
    "Ü": "01010 00000 10001 10001 10001 10001 10001 10001 01110",
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    ":": "00000 00100 00000 00000 00000 00100 00000", ".": "00000 00000 00000 00000 00000 01100 01100",
    ",": "00000 00000 00000 00000 01100 00100 01000", "-": "00000 00000 00000 11111 00000 00000 00000",
    "–": "00000 00000 00000 11111 00000 00000 00000", "/": "00001 00010 00010 00100 01000 01000 10000",
    "(": "00010 00100 01000 01000 01000 00100 00010", ")": "01000 00100 00010 00010 00010 00100 01000",
    "&": "01100 10010 10100 01000 10101 10010 01101", "+": "00000 00100 00100 11111 00100 00100 00000",
    "'": "00100 00100 01000 00000 00000 00000 00000", " ": "00000 00000 00000 00000 00000 00000 00000",
}
_BLACK, _WHITE, _GREY, _LABEL = (30, 30, 30), (255, 255, 255), (200, 200, 200), (90, 90, 90)
_GREEN = (0, 150, 70)  # safety green: rescue signs, escape arrows
_RED = (210, 30, 30)  # fire-protection red: fire extinguisher sign only
_ROUTE = (204, 236, 212)  # pale green band = escape route
_BLUE = (0, 82, 160)  # "you are here"


def _label(s: str) -> str:
    s = s.upper()
    missing = sorted({ch for ch in s if ch not in _FONT})
    if missing:
        raise ValueError(f"Fluchtwegplan: kein Zeichen für {''.join(missing)!r} in {s!r}")
    return s


def text_width(s: str, scale: int = 1) -> int:
    return (6 * len(s) - 1) * scale if s else 0


def _inside_seg(px: int, py: int, ax: int, ay: int, bx: int, by: int, r2: int) -> bool:
    dx, dy, wx, wy = bx - ax, by - ay, px - ax, py - ay
    dot, len2 = wx * dx + wy * dy, dx * dx + dy * dy
    if dot <= 0 or len2 == 0:
        return wx * wx + wy * wy < r2
    if dot >= len2:
        return (px - bx) ** 2 + (py - by) ** 2 < r2
    cross = wx * dy - wy * dx
    return cross * cross < r2 * len2


def _inside_poly(px: int, py: int, pts: tuple) -> bool:
    """Point in a convex polygon (any orientation)."""
    sign = 0
    for (ax, ay), (bx, by) in zip(pts, pts[1:] + pts[:1]):
        c = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        if c:
            if sign and (c > 0) != (sign > 0):
                return False
            sign = c
    return True


class _Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.px = [bytearray(_WHITE * w) for _ in range(h)]
        # (kind, name, (x0, y0, x1, y1)) with exclusive x1/y1 – kinds: wall, text, sign, arrow, area
        self.items: list[tuple[str, str, tuple[int, int, int, int]]] = []

    def note(self, kind: str, name: str, box: tuple[int, int, int, int]) -> None:
        self.items.append((kind, name, box))

    def rect(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        x0, x1 = max(0, x0), min(self.w, x1)
        if x0 >= x1:
            return
        fill = bytes(color) * (x1 - x0)
        for y in range(max(0, y0), min(self.h, y1)):
            self.px[y][3 * x0: 3 * x1] = fill

    def wall(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.rect(x0, y0, x1, y1, _BLACK)
        self.note("wall", "", (x0, y0, x1, y1))

    def text(self, x: int, y: int, s: str, color=_BLACK, scale: int = 1) -> tuple[int, int, int, int]:
        s = _label(s)
        cx, top = x, y
        for ch in s:
            rows = _FONT[ch].split()
            gy = y - (len(rows) - 7) * scale  # umlaut dots go above the cell
            top = min(top, gy)
            for ry, bits in enumerate(rows):
                for rx, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(cx + rx * scale, gy + ry * scale, cx + (rx + 1) * scale,
                                  gy + (ry + 1) * scale, color)
            cx += 6 * scale
        box = (x, top, x + text_width(s, scale), y + 7 * scale)
        self.note("text", s, box)
        return box

    def shapes(self, ox: int, oy: int, size: int, shapes, kind: str = "", name: str = "",
               units_w: int = 100, background=None) -> tuple[int, int, int, int]:
        """Draw ``shapes`` given in sign units: 100 units are ``size`` pixels, origin (ox, oy).
        A pixel centre (x + ½, y + ½) is compared in integer coordinates scaled by 2·size, so no
        rounding happens anywhere. Shapes: ("rect", x0, y0, x1, y1, c), ("circle", cx, cy, r, c),
        ("seg", ax, ay, bx, by, r, c) – a line with round ends –, ("poly", ((x, y), …), c).
        Returns (and notes, if ``kind`` is given) the box of the sign background, or without one
        the box of the pixels actually drawn."""
        k = 2 * size
        if background is not None:
            box = (ox, oy, ox + units_w * size // 100, oy + size)
            self.rect(*box, background)
            drawn = list(box)
        else:
            drawn = [self.w, self.h, 0, 0]
        for shape in shapes:
            form, color = shape[0], shape[-1]
            if form == "rect":
                pts, r = (shape[1:3], shape[3:5]), 0
            elif form == "circle":
                pts, r = (shape[1:3],), shape[3]
            elif form == "seg":
                pts, r = (shape[1:3], shape[3:5]), shape[5]
            else:
                pts, r = tuple(shape[1]), 0
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            x_lo = max(0, ox + (min(xs) - r) * size // 100 - 1)
            x_hi = min(self.w, ox - (-(max(xs) + r) * size // 100) + 1)
            y_lo = max(0, oy + (min(ys) - r) * size // 100 - 1)
            y_hi = min(self.h, oy - (-(max(ys) + r) * size // 100) + 1)
            sp = tuple((x * k, y * k) for x, y in pts)
            r2 = (r * k) ** 2
            rgb = bytes(color)
            for y in range(y_lo, y_hi):
                py = (2 * (y - oy) + 1) * 100
                row = self.px[y]
                for x in range(x_lo, x_hi):
                    px = (2 * (x - ox) + 1) * 100
                    if form == "rect":
                        hit = sp[0][0] <= px <= sp[1][0] and sp[0][1] <= py <= sp[1][1]
                    elif form == "circle":
                        hit = (px - sp[0][0]) ** 2 + (py - sp[0][1]) ** 2 < r2
                    elif form == "seg":
                        hit = _inside_seg(px, py, *sp[0], *sp[1], r2)
                    else:
                        hit = _inside_poly(px, py, sp)
                    if hit:
                        row[3 * x: 3 * x + 3] = rgb
                        drawn[0], drawn[1] = min(drawn[0], x), min(drawn[1], y)
                        drawn[2], drawn[3] = max(drawn[2], x + 1), max(drawn[3], y + 1)
        box = (drawn[0], drawn[1], drawn[2], drawn[3])
        if kind:
            self.note(kind, name, box)
        return box

    def png(self) -> bytes:
        raw = zlib.compress(b"".join(b"\x00" + bytes(r) for r in self.px), 9)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(
                ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)  # 8-bit RGB
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


# ISO 7010 E002 (emergency exit, right) with direction arrow: 200 × 100 units, white on green
_SIGN_EXIT = (
    ("rect", 60, 8, 94, 92, _WHITE), ("rect", 66, 14, 88, 92, _GREEN),  # door, open at the bottom
    ("circle", 46, 17, 9, _WHITE),  # running man heading into the door
    ("seg", 42, 30, 34, 55, 7, _WHITE),
    ("seg", 42, 33, 54, 42, 5, _WHITE), ("seg", 54, 42, 63, 35, 5, _WHITE),
    ("seg", 40, 34, 28, 40, 5, _WHITE), ("seg", 28, 40, 20, 32, 5, _WHITE),
    ("seg", 34, 55, 50, 64, 6, _WHITE), ("seg", 50, 64, 54, 86, 6, _WHITE),
    ("seg", 54, 86, 62, 86, 4, _WHITE),
    ("seg", 34, 55, 24, 70, 6, _WHITE), ("seg", 24, 70, 8, 72, 6, _WHITE),
    ("rect", 112, 42, 158, 58, _WHITE), ("poly", ((152, 22), (188, 50), (152, 78)), _WHITE),
)


def _assembly_shapes() -> tuple:
    """ISO 7010 E007 (assembly point): four arrows from the corners towards a group of people."""
    out = []
    for sx, sy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        def at(d: int) -> tuple[int, int]:
            return 50 + sx * d, 50 + sy * d
        bx, by = at(34)
        out.append(("seg", *at(43), bx, by, 4, _WHITE))
        out.append(("poly", (at(23), (bx + 8 * sy, by - 8 * sx), (bx - 8 * sy, by + 8 * sx)), _WHITE))
    out += [
        ("circle", 50, 35, 7, _WHITE), ("seg", 50, 53, 50, 68, 8, _WHITE),
        ("circle", 34, 44, 6, _WHITE), ("seg", 34, 58, 34, 68, 6, _WHITE),
        ("circle", 66, 44, 6, _WHITE), ("seg", 66, 58, 66, 68, 6, _WHITE),
    ]
    return tuple(out)


_SIGN_ASSEMBLY = _assembly_shapes()
# ISO 7010 F001 (fire extinguisher): white extinguisher with hose and flames on red
_SIGN_EXTINGUISHER = (
    ("seg", 40, 50, 40, 78, 13, _WHITE), ("rect", 35, 26, 45, 38, _WHITE),
    ("seg", 28, 24, 52, 17, 3, _WHITE),
    ("seg", 45, 28, 62, 30, 3, _WHITE), ("seg", 62, 30, 70, 50, 3, _WHITE),
    ("seg", 70, 50, 72, 64, 4, _WHITE),
    ("poly", ((74, 4), (86, 26), (66, 26)), _WHITE), ("poly", ((88, 12), (95, 28), (82, 28)), _WHITE),
)
_YOU_ARE_HERE = (("circle", 50, 50, 48, _BLUE), ("circle", 50, 50, 31, _WHITE),
                 ("circle", 50, 50, 16, _BLUE))
_ARROW_EAST = (("rect", 0, 34, 120, 66, _GREEN), ("poly", ((110, 0), (200, 50), (110, 100)), _GREEN))

PLAN_SIZE = (800, 500)
PLAN_TITLE = "FLUCHTWEGPLAN ERDGESCHOSS"


def _draw_plan(title: str, subtitle: str) -> _Canvas:
    """Ground floor of a school: staircase 1 in the west, classrooms along a corridor, the escape
    route east to the emergency exit, from there north to the assembly point at the northern car
    park, the fire extinguisher next to staircase 1 (matches the fake vision transcription)."""
    c = _Canvas(*PLAN_SIZE)
    c.text(40, 18, title, scale=3)
    if subtitle:
        c.text(40, 50, subtitle, color=_LABEL)
    wall = 4
    x0, y0, x1, y1 = 40, 80, 560, 360  # building
    cy0, cy1 = 195, 245  # corridor walls (outer edges)
    ey0, ey1 = 200, 240  # emergency exit: gap in the east wall
    c.rect(x0 + wall, cy0 + wall, x1, cy1 - wall, _ROUTE)  # escape route band incl. the exit door
    c.note("area", "fluchtweg", (x0 + wall, cy0 + wall, x1, cy1 - wall))
    for box in ((x0, y0, x1, y0 + wall), (x0, y1 - wall, x1, y1), (x0, y0, x0 + wall, y1),
                (x1 - wall, y0, x1, ey0), (x1 - wall, ey1, x1, y1)):
        c.wall(*box)
    partitions = (140, 245, 350, 455)
    for x in partitions:  # room partitions above and below the corridor
        c.wall(x, y0 + wall, x + wall, cy0)
        c.wall(x, cy1, x + wall, y1 - wall)
    for xa in range(x0, x1 - wall, 105):  # corridor walls, a door gap of 35 px per room
        c.wall(xa + 35, cy0, min(xa + 105, x1 - wall), cy0 + wall)
        c.wall(xa + 35, cy1 - wall, min(xa + 105, x1 - wall), cy1)

    # rooms (interior x ranges) and their names
    bounds = (x0 + wall,) + tuple(x for p in partitions for x in (p, p + wall)) + (x1 - wall,)
    rooms = list(zip(bounds[::2], bounds[1::2]))
    for i in range(7):  # staircase 1 (west): steps
        c.rect(52, 92 + i * 10, 132, 94 + i * 10, _GREY)
    c.note("area", "treppe", (52, 92, 132, 154))
    upper = ("TREPPENRAUM 1", "KLASSE 1A", "KLASSE 1B", "KLASSE 2A", "KLASSE 2B")
    lower = ("WC", "LEHRERZIMMER", "KLASSE 3A", "KLASSE 3B", "KLASSE 4A")
    for (ra, rb), up, low in zip(rooms, upper, lower):
        c.text((ra + rb - text_width(up)) // 2, 175 if up.startswith("TREPPE") else 134, up, _LABEL)
        c.text((ra + rb - text_width(low)) // 2, 297, low, _LABEL)

    c.shapes(82, 208, 24, _SIGN_EXTINGUISHER, "sign", "feuerloescher", background=_RED)
    for x in (118, 172, 344, 414, 484):  # escape arrows towards the exit
        c.shapes(x, 211, 18, _ARROW_EAST, "arrow", "fluchtweg", units_w=200)
    c.shapes(222, 210, 20, _YOU_ARE_HERE, "sign", "standort")
    c.text(248, 217, "SIE SIND HIER", color=_BLUE)

    # outside: from the exit east, then north to the assembly point
    c.shapes(0, 0, 100, (("seg", x1, 220, 710, 220, 3, _GREEN),), "arrow", "weg-aussen")
    c.shapes(0, 0, 100, (("seg", 710, 220, 710, 184, 3, _GREEN),
                         ("poly", ((710, 166), (722, 184), (698, 184)), _GREEN)), "arrow", "weg-aussen")
    c.shapes(566, 232, 40, _SIGN_EXIT, "sign", "notausgang", units_w=200, background=_GREEN)
    c.text(566, 278, "NOTAUSGANG OST")
    c.shapes(686, 86, 48, _SIGN_ASSEMBLY, "sign", "sammelplatz", background=_GREEN)
    c.text(710 - text_width("SAMMELPLATZ") // 2, 140, "SAMMELPLATZ")
    c.text(710 - text_width("PARKPLATZ NORD") // 2, 151, "PARKPLATZ NORD")

    # north arrow
    c.text(40, 400, "N", scale=2)
    c.shapes(0, 0, 100, (("rect", 62, 392, 66, 420, _BLACK),
                         ("poly", ((64, 380), (72, 394), (56, 394)), _BLACK)), "sign", "nordpfeil")

    # legend
    lx0, ly0, lx1, ly1 = 90, 372, 790, 490
    for box in ((lx0, ly0, lx1, ly0 + 1), (lx0, ly1 - 1, lx1, ly1), (lx0, ly0, lx0 + 1, ly1),
                (lx1 - 1, ly0, lx1, ly1)):
        c.rect(*box, _BLACK)
        c.note("wall", "legende", box)
    c.text(100, 380, "LEGENDE")
    col1, col2, col3, row1, row2 = 100, 330, 560, 394, 442  # icons are 32 px high
    c.rect(col1, row1 + 4, col1 + 64, row1 + 28, _ROUTE)
    c.note("area", "legende-fluchtweg", (col1, row1 + 4, col1 + 64, row1 + 28))
    c.shapes(col1 + 14, row1 + 7, 18, _ARROW_EAST, "arrow", "legende-fluchtweg", units_w=200)
    c.text(col1 + 74, row1 + 13, "FLUCHTWEG")
    c.shapes(col2, row1, 32, _SIGN_EXIT, "sign", "notausgang", units_w=200, background=_GREEN)
    c.text(col2 + 74, row1 + 13, "NOTAUSGANG")
    c.shapes(col3, row1, 32, _SIGN_ASSEMBLY, "sign", "sammelplatz", background=_GREEN)
    c.text(col3 + 74, row1 + 13, "SAMMELPLATZ")
    c.shapes(col1, row2, 32, _SIGN_EXTINGUISHER, "sign", "feuerloescher", background=_RED)
    c.text(col1 + 74, row2 + 13, "FEUERLÖSCHER")
    c.shapes(col2 + 4, row2 + 4, 24, _YOU_ARE_HERE, "sign", "standort")
    c.text(col2 + 74, row2 + 13, "STANDORT")
    return c


def plan_layout(title: str = PLAN_TITLE, subtitle: str = "") -> list[tuple[str, str, tuple]]:
    """Every wall, label, sign and arrow of the escape plan with its pixel box (for tests)."""
    return list(_draw_plan(title, subtitle).items)


@functools.lru_cache(maxsize=8)
def plan_png(title: str = PLAN_TITLE, subtitle: str = "") -> bytes:
    """The escape plan as RGB PNG (byte-identical on every run; cached, the image is immutable)."""
    return _draw_plan(title, subtitle).png()
