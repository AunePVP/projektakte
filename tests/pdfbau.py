"""A small PDF builder for the tests of the PDF reader (pdfseiten.py): pages with real text (Helvetica, WinAnsi)
placed line by line, page images, /Rotate and crop boxes – written by hand, so every byte is known."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw


def _esc(text: str) -> bytes:
    raw = text.encode("cp1252")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def line(x: float, y: float, text: str, *, size: float = 11, bold: bool = False, mode: int = 0) -> bytes:
    """One line of text with its left end at (x, y), in points from the lower left corner of the page.
    ``mode`` 3: invisible (the text layer of a scan)."""
    return (b"BT /%s %g Tf %d Tr 1 0 0 1 %g %g Tm (%s) Tj ET"
            % (b"F2" if bold else b"F1", size, mode, x, y, _esc(text)))


def runs(y: float, parts: list[tuple[float, str]], *, size: float = 11, extra: bytes = b"") -> bytes:
    """One line made of several runs, each placed on its own (as justified text is written by many producers).
    ``extra``: text state operators before the runs (e.g. b"0.6 Tc")."""
    body = b" ".join(b"1 0 0 1 %g %g Tm (%s) Tj" % (x, y, _esc(text)) for x, text in parts)
    return b"BT /F1 %g Tf %s %s ET" % (size, extra, body)


def scan_image(size=(1240, 1754), text: str = "Gescannte Seite") -> bytes:
    """A grey JPEG that looks like a scanned page."""
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    for y in range(80, size[1] - 80, 90):
        d.text((80, y), text, fill=0)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=60)
    return out.getvalue()


class Pdf:
    def __init__(self):
        self.objects: list[bytes] = []
        self.pages: list[bytes] = []  # page dictionaries without /Parent
        self.f1 = self.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        self.f2 = self.add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def stream(self, head: bytes, data: bytes) -> int:
        return self.add(b"<< " + head + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    def page(self, *content: bytes, size=(595, 842), rotate: int = 0, crop: tuple | None = None,
             image: tuple[bytes, tuple[int, int], tuple[float, float, float, float]] | None = None) -> "Pdf":
        """A page drawing ``content`` (see :func:`line`); ``image``: (JPEG bytes, pixel size, (x, y, width, height)
        on the page) drawn first."""
        ops = list(content)
        xobjects = b""
        if image is not None:
            data, (w, h), (x, y, bw, bh) = image
            ref = self.stream(b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                              b"/BitsPerComponent 8 /Filter /DCTDecode" % (w, h), data)
            xobjects = b" /XObject << /Im1 %d 0 R >>" % ref
            ops.insert(0, b"q %g 0 0 %g %g %g cm /Im1 Do Q" % (bw, bh, x, y))
        cid = self.stream(b"", b"\n".join(ops))
        extra = b""
        if rotate:
            extra += b" /Rotate %d" % rotate
        if crop:
            extra += b" /CropBox [%g %g %g %g]" % crop
        self.pages.append(b"/MediaBox [0 0 %g %g]%s /Contents %d 0 R /Resources << /Font << /F1 %d 0 R /F2 %d 0 R >>%s >>"
                          % (size[0], size[1], extra, cid, self.f1, self.f2, xobjects))
        return self

    def build(self) -> bytes:
        objects = list(self.objects)
        pages_id = len(objects) + len(self.pages) + 1
        kids = []
        for body in self.pages:
            objects.append(b"<< /Type /Page /Parent %d 0 R %s >>" % (pages_id, body))
            kids.append(len(objects))
        objects.append(b"<< /Type /Pages /Kids [%s] /Count %d >>"
                       % (b" ".join(b"%d 0 R" % k for k in kids), len(kids)))
        objects.append(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for i, obj in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
        out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objects) + 1, len(objects), xref))
        return bytes(out)


def scan_pdf(pages: int = 1, layer: str | None = None) -> bytes:
    """Scanned pages: an image over the whole page, with ``layer`` as an invisible OCR text layer."""
    pdf = Pdf()
    for k in range(pages):
        content = [line(60, 780, layer, mode=3)] if layer else []
        pdf.page(*content, image=(scan_image(text=f"Gescannte Seite {k + 1}"), (1240, 1754), (0, 0, 595, 842)))
    return pdf.build()
