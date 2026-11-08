"""Byte-deterministic writers for PDF, DOCX and PNG (standard library only).

The demo data must be reproducible down to the file hash, so no timestamps are embedded.
"""
from __future__ import annotations

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
_FONT = {  # 5×7 bitmap font, enough for the plan labels
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "K": "10001 10010 10100 11000 10100 10010 10001",
    "L": "10000 10000 10000 10000 10000 10000 11111", "M": "10001 11011 10101 10101 10001 10001 10001",
    "N": "10001 11001 10101 10011 10001 10001 10001", "O": "01110 10001 10001 10001 10001 10001 01110",
    "P": "11110 10001 10001 11110 10000 10000 10000", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "W": "10001 10001 10001 10101 10101 10101 01010",
    "X": "10001 10001 01010 00100 01010 10001 10001", "Z": "11111 00001 00010 00100 01000 10000 11111", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", ":": "00000 00100 00000 00000 00000 00100 00000",
    "-": "00000 00000 00000 11111 00000 00000 00000", " ": "00000 00000 00000 00000 00000 00000 00000",
}
_BLACK, _WHITE, _GREEN, _RED, _GREY = (30, 30, 30), (255, 255, 255), (0, 150, 70), (210, 30, 30), (200, 200, 200)


class _Canvas:
    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.px = [bytearray(_WHITE * w) for _ in range(h)]

    def rect(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        for y in range(max(0, y0), min(self.h, y1)):
            row = self.px[y]
            for x in range(max(0, x0), min(self.w, x1)):
                row[3 * x: 3 * x + 3] = bytes(color)

    def text(self, x: int, y: int, s: str, color=_BLACK, scale: int = 1) -> None:
        for ch in s.upper():
            rows = _FONT.get(ch, _FONT[" "]).split()
            for ry, bits in enumerate(rows):
                for rx, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(x + rx * scale, y + ry * scale, x + (rx + 1) * scale, y + (ry + 1) * scale, color)
            x += 6 * scale

    def arrow_east(self, x: int, y: int, color=_GREEN) -> None:
        self.rect(x, y - 3, x + 22, y + 3, color)
        for i in range(10):
            self.rect(x + 22 + i, y - 10 + i, x + 23 + i, y + 10 - i, color)

    def png(self) -> bytes:
        raw = zlib.compress(b"".join(b"\x00" + bytes(r) for r in self.px), 9)

        def chunk(tag: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + tag + data + struct.pack(
                ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)  # 8-bit RGB
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def plan_png(title: str = "FLUCHTWEGPLAN ERDGESCHOSS") -> bytes:
    """A recognisable escape plan of a ground floor: rooms along a corridor, staircase 1 in the west,
    green escape arrows to the emergency exit east, fire extinguisher next to staircase 1, assembly
    point at the northern car park (matches the fake vision transcription)."""
    c = _Canvas(680, 420)
    c.text(40, 20, title, scale=3)
    wall = 4
    x0, y0, x1, y1 = 40, 80, 560, 360  # building
    cy0, cy1 = 195, 245  # corridor
    for (a, b, d, e) in ((x0, y0, x1, y0 + wall), (x0, y1 - wall, x1, y1), (x0, y0, x0 + wall, y1),
                         (x1 - wall, y0, x1, cy0 + 5), (x1 - wall, cy1 - 5, x1, y1)):
        c.rect(a, b, d, e, _BLACK)  # outer walls, gap in the east wall = emergency exit
    for x in (140, 245, 350, 455):  # room partitions above and below the corridor, doors left open
        c.rect(x, y0, x + wall, cy0, _BLACK)
        c.rect(x, cy1, x + wall, y1, _BLACK)
    for xa in range(x0, x1, 105):  # corridor walls with doors
        c.rect(xa + 35, cy0, xa + 105, cy0 + wall, _BLACK)
        c.rect(xa + 35, cy1 - wall, xa + 105, cy1, _BLACK)
    for i in range(8):  # staircase 1 (west): steps
        c.rect(52, 96 + i * 11, 132, 98 + i * 11, _GREY)
    c.text(62, 186 - 20, "TR 1", scale=2)
    c.rect(150, 205, 166, 228, _RED)  # fire extinguisher next to staircase 1
    c.text(154, 210, "F", color=_WHITE, scale=2)
    for x in (190, 290, 390, 480):  # escape route east
        c.arrow_east(x, 220)
    c.rect(566, 200, 626, 240, _GREEN)  # emergency exit sign
    c.text(574, 213, "EXIT", color=_WHITE, scale=2)
    c.text(566, 250, "NOTAUSGANG", scale=1)
    c.text(566, 260, "OST", scale=1)
    c.rect(575, 90, 625, 140, _GREEN)  # assembly point
    c.rect(597, 100, 603, 130, _WHITE)
    c.rect(585, 112, 615, 118, _WHITE)
    c.text(560, 146, "SAMMELPLATZ", scale=1)
    c.text(560, 156, "PARKPLATZ NORD", scale=1)
    c.rect(300, 230, 310, 240, _RED)  # you are here
    c.text(280, 262, "SIE SIND HIER", color=_RED, scale=1)
    c.text(40, 392, "N", scale=2)  # north arrow
    c.rect(56, 380, 60, 408, _BLACK)
    for i in range(7):
        c.rect(58 - i, 378 + i, 59 + i, 379 + i, _BLACK)
    c.arrow_east(200, 395)
    c.text(240, 392, "FLUCHTWEG", scale=1)
    c.rect(330, 388, 342, 402, _RED)
    c.text(350, 392, "FEUERLOESCHER", scale=1)
    return c.png()
