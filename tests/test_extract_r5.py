"""Round 5: reading scan pages is bounded against crafted PDFs. Images are told apart by their PDF object
(one object under many names or in many forms is one image), the limits use the real size of JPEG data,
the content walk and putting a page together are bounded, one document reads each image once and has
bounds of its own, and large JPEGs are decoded at reduced scale (memory of an A0 plan)."""
import io
import json
import resource
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw, features

from projektakte import extract as extract_mod
from projektakte.extract import extract

ROOT = Path(__file__).resolve().parent.parent


# -- a PDF writer with shared objects, forms and inline images -------------------------------------------
class Doc:
    """Minimal PDF: objects are added and referenced by number; pages are added with their content and
    XObject resources (name -> object number)."""

    def __init__(self):
        self.objects: list[bytes] = []
        self.pages: list[tuple[int, bytes]] = []

    def add(self, body: bytes) -> int:
        self.objects.append(body)
        return len(self.objects)

    def stream(self, head: bytes, data: bytes) -> int:
        return self.add(b"<< " + head + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream")

    def image(self, data: bytes, size, *, filt=b"/FlateDecode", cs=b"/DeviceGray", bpc=8, declared=None) -> int:
        w, h = declared or size
        head = b"/Type /XObject /Subtype /Image"
        if w is not None:
            head += b" /Width %d /Height %d" % (w, h)
        return self.stream(head + b" /ColorSpace %s /BitsPerComponent %d /Filter %s" % (cs, bpc, filt), data)

    def form(self, content: bytes, xobjects: dict[str, int], matrix=b"[1 0 0 1 0 0]") -> int:
        return self.stream(b"/Type /XObject /Subtype /Form /BBox [0 0 2000 2000] /Matrix " + matrix
                           + b" /Resources " + resources(xobjects), content)

    def page(self, content: bytes, xobjects: dict[str, int], *, compress: bool = False) -> None:
        if compress:
            cid = self.stream(b"/Filter /FlateDecode", zlib.compress(content, 9))
        else:
            cid = self.stream(b"", content)
        self.pages.append((cid, resources(xobjects)))

    def page_with(self, content_id: int, xobjects: dict[str, int]) -> None:
        self.pages.append((content_id, resources(xobjects)))

    def build(self) -> bytes:
        objects = list(self.objects)
        pages_id = len(objects) + len(self.pages) + 1
        kids = []
        for cid, res in self.pages:
            objects.append(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources %s >>"
                           % (pages_id, cid, res))
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


def resources(xobjects: dict[str, int]) -> bytes:
    return b"<< /XObject << " + b" ".join(b"/%s %d 0 R" % (n.encode(), i) for n, i in xobjects.items()) + b" >> >>"


def draw(name: str, m) -> bytes:
    return b"q %s cm /%s Do Q" % (" ".join(f"{v:g}" for v in m).encode(), name.encode())


def scan(size=(600, 400), mode="L", text="Brandabschnitt 2 – Rauchabzug") -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    for y in range(20, size[1] - 20, max(20, size[1] // 12)):
        d.text((20, y), text, fill="black")
    d.rectangle([2, 2, size[0] - 3, size[1] - 3], outline=(200, 0, 0), width=3)
    return img.convert(mode)


def jpeg(img: Image.Image, quality=85) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def flate(img: Image.Image) -> bytes:
    return zlib.compress(img.convert("L").tobytes())


class Vision:
    vision = True
    model = "vision-r5"

    def __init__(self, answer="Transkript: Rauchabzug"):
        self.answer = answer
        self.images: list[Image.Image] = []

    def describe_image(self, data, mime, prompt):
        img = Image.open(io.BytesIO(data))
        img.load()
        assert mime == {"JPEG": "image/jpeg", "PNG": "image/png"}[img.format]
        self.images.append(img)

        class Result:
            text = f"{self.answer} {len(self.images)}"
        return Result()


@pytest.fixture()
def decodes(monkeypatch):
    """Counts the images decoded (by PDF object identity) during an extraction."""
    seen: list[tuple] = []
    real = extract_mod._decode

    def counting(image, target):
        seen.append(image.ident)
        return real(image, target)

    monkeypatch.setattr(extract_mod, "_decode", counting)
    return seen


def grid(n: int, size: float = 25):
    return [(size, 0, 0, size, 20 + (k % 20) * (size + 2), 20 + (k // 20) * (size + 2)) for k in range(n)]


# -- one image object under many names, in many forms -------------------------------------------------------
def test_one_image_under_many_names_and_forms_is_decoded_and_sent_once(decodes):
    """Before: dedup by resource name – 200 names of one 3000×3000 object were decoded 16 times (until the
    declared 150 MP were used up), 777 MB RSS for RGB, and the page was "184 Bild(er) nicht ausgewertet"."""
    doc = Doc()
    img = doc.image(flate(scan((3000, 3000))), (3000, 3000))
    names = {f"X{k}": img for k in range(200)}
    forms = {f"F{k}": doc.form(b"/Im1 Do", {"Im1": img}) for k in range(200)}
    content = b"\n".join([draw(n, m) for n, m in zip(names, grid(200))]
                         + [draw(n, (20, 0, 0, 20, 300 + k % 10, 600)) for k, n in enumerate(forms)])
    doc.page(content, {**names, **forms})
    llm = Vision()
    started = time.monotonic()
    r = extract(Path("Alias.pdf"), doc.build(), llm=llm)
    assert time.monotonic() - started < 5
    assert (r.status, r.incomplete, r.message) == ("ok", False, None)
    assert len(decodes) == 1 and len(llm.images) == 1  # one image: sent alone, not put together
    assert llm.images[0].size == (3000, 3000)


def test_jpeg_declared_smaller_counts_with_its_real_size(decodes):
    """Five different 6000×6000 JPEGs declared as 1×1: the real sizes (read from the JPEG headers) fill the
    page limit after four; before, all were decoded (1.5 GB for twenty, no note)."""
    doc = Doc()
    base = scan((6000, 6000), text="Grundriss")
    ids = {}
    for k in range(5):
        d = base.copy()
        ImageDraw.Draw(d).rectangle([100 + 50 * k, 100, 140 + 50 * k, 140], fill=0)  # different data
        ids[f"Im{k}"] = doc.image(jpeg(d, 30), (6000, 6000), filt=b"/DCTDecode", declared=(1, 1))
    doc.page(b"\n".join(draw(n, (100, 0, 0, 100, 20 + 110 * k, 300)) for k, n in enumerate(ids)), ids)
    llm = Vision()
    r = extract(Path("Luege.pdf"), doc.build(), llm=llm)
    assert len(decodes) == 4 and len(llm.images) == 1  # four put together into one page image
    assert (r.status, r.incomplete, r.retry) == ("ok", True, False)
    assert r.message == ("1 Seite(n) nur teilweise gelesen (1 Bild(er) nicht ausgewertet (mehr als 200 Bilder "
                         "bzw. 150 Megapixel je Seite))")


def test_huge_jpeg_header_behind_a_tiny_declaration_is_not_decoded(decodes):
    """A 20000×20000 JPEG header (Pillow refuses it as a decompression bomb) declared 1×1: too large, no
    retry – before it was decoded per name."""
    doc = Doc()
    big = jpeg(Image.new("L", (20000, 20000), 255), 10)
    ids = {f"X{k}": doc.image(big, (20000, 20000), filt=b"/DCTDecode", declared=(1, 1)) for k in range(1)}
    ids.update({f"Y{k}": ids["X0"] for k in range(30)})
    doc.page(b"\n".join(draw(n, (50, 0, 0, 50, 20 + 3 * k, 300)) for k, n in enumerate(ids)), ids)
    llm = Vision()
    r = extract(Path("Bombe.pdf"), doc.build(), llm=llm)
    assert decodes == [] and llm.images == []
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert r.message == "Scan ohne Textebene – Bilder zu groß (über 150 Megapixel je Seite)"


def test_image_without_declared_size_is_not_decoded(decodes):
    """Raw samples without /Width and /Height: the size is unknown – never counted as 0."""
    doc = Doc()
    ids = {"Im1": doc.image(zlib.compress(b"\xff" * 3000 * 3000), (None, None))}
    doc.page(draw("Im1", (300, 0, 0, 300, 50, 50)), ids)
    r = extract(Path("x.pdf"), doc.build(), llm=Vision())
    assert decodes == []
    assert (r.status, r.incomplete, r.retry) == ("skipped", True, False)
    assert "Bilder zu groß" in r.message


# -- listed images when positions are unknown ------------------------------------------------------------------
def test_listed_images_keep_the_page_limits(monkeypatch, decodes):
    """A page whose content draws nothing: its listed images are read – within the limits (real sizes of
    JPEGs declared 1×1, identity) instead of all of them; before, the limits were skipped (size 0)."""
    monkeypatch.setattr(extract_mod, "MAX_PAGE_PIXELS", 1_000_000)
    doc = Doc()
    ids = {}
    for k in range(10):
        img = scan((600, 600), text=f"Blatt {k}")
        ids[f"Im{k}"] = doc.image(jpeg(img), (600, 600), filt=b"/DCTDecode", declared=(1, 1))
    ids.update({f"Alias{k}": ids["Im0"] for k in range(20)})  # the first one again, under 20 names
    doc.page(b"q Q", ids)
    llm = Vision()
    r = extract(Path("Liste.pdf"), doc.build(), llm=llm)
    assert len(decodes) == 2 and len(llm.images) == 2  # 2 × 0.36 MP fit into 1 MP
    assert r.incomplete and not r.retry
    assert "8 Bild(er) nicht ausgewertet (mehr als 200 Bilder bzw. 1 Megapixel je Seite)" in r.message


def test_odd_matrix_values_read_the_drawn_images_unplaced():
    """A malformed cm no longer sends the walk to the resource list: the drawn images are read unplaced,
    the unused resource is not."""
    doc = Doc()
    drawn = doc.image(flate(scan((300, 300))), (300, 300))
    unused = doc.image(flate(scan((500, 500))), (500, 500))
    doc.page(b"1 0 0 1 0 /Bogus cm\n" + draw("Im1", (300, 0, 0, 300, 50, 50)), {"Im1": drawn, "Unused": unused})
    llm = Vision()
    r = extract(Path("x.pdf"), doc.build(), llm=llm)
    assert (r.status, r.incomplete) == ("ok", False)
    assert [i.size for i in llm.images] == [(300, 300)]


# -- bounded content walk ----------------------------------------------------------------------------------------
def test_many_placements_stop_at_the_bound_in_linear_time(decodes):
    """20,000 positions of one small image: before, the placement dedup was a list search (4.8 s, growing
    with the square of the count) and the walk had no bound."""
    doc = Doc()
    ids = {"Im1": doc.image(flate(scan((50, 50))), (50, 50))}
    ops = b"\n".join(draw("Im1", (0.5, 0, 0, 0.5, (k % 1000) * 0.5, (k // 1000) * 0.5)) for k in range(20_000))
    doc.page(ops, ids, compress=True)
    data = doc.build()
    assert len(data) < 100_000
    llm = Vision()
    started = time.monotonic()
    r = extract(Path("Positionen.pdf"), data, llm=llm)
    assert time.monotonic() - started < 3
    assert len(decodes) == 1 and len(llm.images) == 1
    assert (r.status, r.incomplete, r.retry) == ("ok", True, False)
    assert r.message == ("1 Seite(n) nur teilweise gelesen (Inhalt von 1 Seite(n) nicht vollständig durchsucht "
                         "(mehr als 4 MB Inhalt, 1.000.000 Zeichenbefehle, 1.000 Formularaufrufe bzw. 2.000 "
                         "Bildpositionen je Seite oder 32 MB Inhalt je Dokument))")


def test_the_same_place_drawn_again_is_one_placement():
    doc = Doc()
    ids = {"Im1": doc.image(flate(scan((50, 50))), (50, 50))}
    doc.page(b"\n".join(draw("Im1", (100, 0, 0, 100, 50, 50)) for _ in range(20_000)), ids, compress=True)
    from pypdf import PdfReader

    drawing = extract_mod._drawn_images(PdfReader(io.BytesIO(doc.build())).pages[0])
    assert (len(drawing.images), drawing.placements, drawing.truncated) == (1, 1, False)


def nested_forms(fanout: int, depth: int = 6, *, moved: bool = True) -> bytes:
    """Form F1 draws F2 ``fanout`` times (at different places unless ``moved`` is false), … the last one
    draws the image: fanout ** depth drawings from a 3 KB file."""
    doc = Doc()
    img = doc.image(flate(scan((10, 10))), (10, 10))
    child, name = img, "Im"
    for level in range(depth, 0, -1):
        step = fanout ** (depth - level)
        content = b"\n".join(draw(name, (1, 0, 0, 1, j * step * 0.01 * moved, j * 0.01 * moved))
                             for j in range(fanout))
        child, name = doc.form(content, {name: child}), "F"
    doc.page(draw("F", (1, 0, 0, 1, 0, 0)), {"F": child})
    return doc.build()


def test_nested_forms_stop_at_the_form_bound():
    """7 forms deep with fan-out 7 = 117,649 drawings from 3 KB: before, about 3 minutes (the walk followed
    every drawing, the dedup was quadratic). Each form is parsed once; the walk stops at MAX_PAGE_FORMS."""
    from pypdf import PdfReader

    for moved in (True, False):  # different places: the placement bound; one place: the form bound
        data = nested_forms(7, moved=moved)
        assert len(data) < 5000
        page = PdfReader(io.BytesIO(data)).pages[0]
        started = time.monotonic()
        drawing = extract_mod._drawn_images(page)
        assert time.monotonic() - started < 1
        assert drawing.truncated and len(drawing.images) == 1
        if moved:
            assert drawing.placements == extract_mod.MAX_PAGE_PLACEMENTS
        else:
            assert drawing.placements == 1 and drawing.forms == extract_mod.MAX_PAGE_FORMS + 1
    llm = Vision()
    started = time.monotonic()
    r = extract(Path("Formulare.pdf"), data, llm=llm)
    assert time.monotonic() - started < 10  # mostly pypdf's own text extraction (bounded at 5000 forms)
    assert len(llm.images) == 1 and r.incomplete and not r.retry and "nicht vollständig durchsucht" in r.message


def test_operation_bound_with_a_form_drawn_again_and_again(monkeypatch):
    """A form with many operations drawn many times: parsed once, walked until MAX_PAGE_OPERATIONS."""
    monkeypatch.setattr(extract_mod, "MAX_PAGE_OPERATIONS", 50_000)
    doc = Doc()
    img = doc.image(flate(scan((20, 20))), (20, 20))
    form = doc.form(b"q Q\n" * 5000 + b"/Im Do", {"Im": img})
    doc.page(b"\n".join(draw("F", (1, 0, 0, 1, k, 0)) for k in range(900)), {"F": form})
    from pypdf import PdfReader

    started = time.monotonic()
    drawing = extract_mod._drawn_images(PdfReader(io.BytesIO(doc.build())).pages[0])
    assert time.monotonic() - started < 2
    assert drawing.truncated and drawing.operations == 50_001 and drawing.forms < 900


def test_huge_content_is_not_parsed_and_the_document_content_is_bounded(monkeypatch):
    """A few KB of compressed content decode to megabytes that pypdf would parse in full (about 1 s per
    MB) – on every page that shares them. The page bound skips such content without parsing it; the
    document bound stops parsing after MAX_DOC_CONTENT_BYTES; the images are then read from the resources."""
    doc = Doc()
    img = doc.image(flate(scan((200, 200))), (200, 200))
    junk = b"q 1 0 0 1 0 0 cm Q\n" * 250_000 + draw("Im1", (200, 0, 0, 200, 50, 50))
    junk = doc.stream(b"/Filter /FlateDecode", zlib.compress(junk, 9))
    doc.page_with(junk, {"Im1": img})  # 4.75 MB decoded: over MAX_PAGE_CONTENT_BYTES
    data = doc.build()
    assert len(data) < 30_000
    from pypdf import PdfReader

    started = time.monotonic()
    drawing = extract_mod._drawn_images(PdfReader(io.BytesIO(data)).pages[0])
    assert time.monotonic() - started < 0.5  # not parsed (pypdf's text extraction still parses it once)
    assert drawing.truncated and drawing.content_bytes == 0 and not drawing.images
    llm = Vision()
    r = extract(Path("Inhalt.pdf"), data, llm=llm)
    assert len(llm.images) == 1 and r.incomplete and not r.retry and "nicht vollständig durchsucht" in r.message

    monkeypatch.setattr(extract_mod, "MAX_DOC_CONTENT_BYTES", 1_000_000)
    doc = Doc()
    img = doc.image(flate(scan((200, 200))), (200, 200))
    shared = b"q Q\n" * 100_000 + draw("Im1", (200, 0, 0, 200, 50, 50))
    shared = doc.stream(b"/Filter /FlateDecode", zlib.compress(shared, 9))
    for _ in range(6):  # 400 KB each: two pages parsed, then the document bound is used up
        doc.page_with(shared, {"Im1": img})
    llm = Vision()
    r = extract(Path("Geteilt.pdf"), doc.build(), llm=llm)
    assert [p.number for p in r.pages] == [1, 2, 3, 4, 5, 6]
    assert len(llm.images) == 1  # one image object: read once for all pages
    assert r.message.startswith("4 Seite(n) nur teilweise gelesen (Inhalt von 4 Seite(n) nicht vollständig")


def test_heavily_overlapping_placements_are_not_put_together(decodes):
    """One image drawn over the whole page at 300 different sizes, next to three others: pasting and
    rescaling that would be far more than one page image – the largest images are sent instead."""
    doc = Doc()
    ids = {f"Im{k}": doc.image(flate(scan((400 + 10 * k, 300))), (400 + 10 * k, 300)) for k in range(4)}
    ops = [draw("Im0", (500 - k, 0, 0, 700 - k, 20, 20)) for k in range(300)]
    ops += [draw(f"Im{k}", (100, 0, 0, 100, 100 * k, 750)) for k in range(1, 4)]
    doc.page(b"\n".join(ops), ids)
    llm = Vision()
    started = time.monotonic()
    r = extract(Path("Ueberlagert.pdf"), doc.build(), llm=llm)
    assert time.monotonic() - started < 3
    assert len(llm.images) == 3 and len(decodes) == 3
    assert sorted(i.size[0] for i in llm.images) == [410, 420, 430]
    assert "1 Bild(er) nicht ausgewertet (mehr als 3 Bilder je Seite, nicht zu einem Seitenbild" in r.message


# -- inline images ---------------------------------------------------------------------------------------------
def inline(img: Image.Image, *, dct=False, declared=None) -> bytes:
    w, h = declared or img.size
    if dct:
        return b"BI /W %d /H %d /CS /G /BPC 8 /F /DCT ID " % (w, h) + jpeg(img) + b"\nEI"
    return b"BI /W %d /H %d /CS /G /BPC 8 /F /Fl ID " % (w, h) + zlib.compress(img.tobytes()) + b"\nEI"


def test_inline_images_are_read_once_also_inside_forms(decodes):
    """The same inline image drawn 40 times is one image; inline images inside a form are read too;
    pypdf's page.images (which decodes every inline image of the page at the first access) is not used."""
    doc = Doc()
    logo = scan((200, 100), text="Logo")
    page_img = scan((400, 300), text="Auflage 3")
    form = doc.form(b"q 300 0 0 200 0 0 cm " + inline(page_img) + b" Q", {})
    ops = [b"q 50 0 0 25 %d 780 cm " % (10 + 12 * k) + inline(logo) + b" Q" for k in range(40)]
    ops.append(b"q 1 0 0 1 100 100 cm /Fm Do Q")
    doc.page(b"\n".join(ops), {"Fm": form})
    llm = Vision()
    r = extract(Path("Inline.pdf"), doc.build(), llm=llm)
    assert (r.status, r.incomplete, r.message) == ("ok", False, None)
    assert len(decodes) == 2 and len(llm.images) == 2
    assert sorted(i.size for i in llm.images) == [(200, 100), (400, 300)]


def test_inline_jpeg_counts_with_its_real_size(monkeypatch, decodes):
    monkeypatch.setattr(extract_mod, "MAX_PAGE_PIXELS", 100_000)
    doc = Doc()
    doc.page(b"q 300 0 0 300 50 50 cm " + inline(scan((400, 400)), dct=True, declared=(1, 1)) + b" Q", {})
    r = extract(Path("x.pdf"), doc.build(), llm=Vision())
    assert decodes == [] and "Bilder zu groß" in r.message


# -- one document: each image once, bounds of its own --------------------------------------------------------------
def test_the_same_image_on_many_pages_is_decoded_and_sent_once(decodes):
    """30 pages drawing one 6000×6000 JPEG under four names each: before, 30 model calls and 41 s."""
    doc = Doc()
    img = doc.image(jpeg(scan((6000, 6000), text="Deckblatt"), 30), (6000, 6000), filt=b"/DCTDecode")
    names = {f"Im{k}": img for k in range(4)}
    content = doc.stream(b"", b"\n".join(draw(n, (100, 0, 0, 100, 10 + 110 * k, 10)) for k, n in enumerate(names)))
    for _ in range(30):
        doc.page_with(content, names)
    llm = Vision()
    started = time.monotonic()
    r = extract(Path("Seiten.pdf"), doc.build(), llm=llm)
    assert time.monotonic() - started < 5
    assert len(decodes) == 1 and len(llm.images) == 1
    assert (r.status, r.incomplete, r.message) == ("ok", False, None)
    assert [p.number for p in r.pages] == list(range(1, 31))
    assert all(p.text == "Transkript: Rauchabzug 1" for p in r.pages)


def test_a_page_image_put_together_again_is_read_once(decodes):
    from .test_daten_r3_scans import page_scan, strips

    doc = Doc()
    parts = strips(page_scan(), 6)
    ids = {f"S{k}": doc.image(jpeg(s.convert("RGB")), s.size, filt=b"/DCTDecode", cs=b"/DeviceRGB")
           for k, (s, _m) in enumerate(parts)}
    content = doc.stream(b"", b"\n".join(draw(n, m) for n, (_s, m) in zip(ids, parts)))
    for _ in range(5):
        doc.page_with(content, ids)
    llm = Vision()
    r = extract(Path("Streifen.pdf"), doc.build(), llm=llm)
    assert len(llm.images) == 1 and len(decodes) == 6
    assert len(r.pages) == 5 and not r.incomplete


def test_document_bounds_leave_pages_out_with_a_note(monkeypatch):
    monkeypatch.setattr(extract_mod, "MAX_DOC_VISION_CALLS", 3)
    doc = Doc()
    for k in range(6):
        image = doc.image(flate(scan((300, 300), text=f"S{k}")), (300, 300))
        doc.page(draw("Im1", (300, 0, 0, 300, 50, 50)), {"Im1": image})
    llm = Vision()
    r = extract(Path("Akte.pdf"), doc.build(), llm=llm)
    assert len(llm.images) == 3 and [p.number for p in r.pages] == [1, 2, 3]
    assert (r.status, r.incomplete, r.retry) == ("ok", True, False)
    assert r.message == ("3 Seite(n) ohne Textebene nicht gelesen (Dokument zu umfangreich (mehr als 3 "
                         "Bildauswertungen bzw. 4.000 Megapixel je Dokument))")


def test_document_pixel_bound(monkeypatch, decodes):
    monkeypatch.setattr(extract_mod, "MAX_DOC_PIXELS", 1_000_000)
    doc = Doc()
    for k in range(8):  # 0.36 MP each, charged at least 0.25 MP: two pages fit
        image = doc.image(flate(scan((600, 600), text=f"S{k}")), (600, 600))
        doc.page(draw("Im1", (300, 0, 0, 300, 50, 50)), {"Im1": image})
    r = extract(Path("Akte.pdf"), doc.build(), llm=Vision())
    assert len(decodes) == 2 and [p.number for p in r.pages] == [1, 2]
    assert r.incomplete and not r.retry
    assert "6 Seite(n) ohne Textebene nicht gelesen (Dokument zu umfangreich" in r.message


def test_a_broken_image_on_many_pages_is_tried_once(decodes):
    doc = Doc()
    broken = doc.image(b"\x00\x01 kein Flate", (100, 100))
    for _ in range(20):
        doc.page(draw("Im1", (100, 0, 0, 100, 50, 50)), {"Im1": broken})
    r = extract(Path("x.pdf"), doc.build(), llm=Vision())
    assert len(decodes) == 1
    assert (r.status, r.incomplete, r.retry) == ("error", True, True)
    assert r.message.startswith("Scan ohne Textebene – Bilder nicht lesbar")


# -- large JPEGs decoded at reduced scale ----------------------------------------------------------------------
def test_large_jpeg_strips_are_decoded_at_reduced_scale_and_put_together(monkeypatch):
    """Strips of a 2400×2400 scan on a page image of 600×600: each strip is decoded at 1/4 scale."""
    from .test_daten_r3_scans import similar

    monkeypatch.setattr(extract_mod, "MAX_IMAGE_PIXELS", 360_000)
    original = scan((2400, 2400), mode="RGB", text="Treppenraum")
    loaded = []
    real = extract_mod._decode_reduced

    def spy(stream, target):
        img = real(stream, target)
        loaded.append((target, img.size))
        return img
    monkeypatch.setattr(extract_mod, "_decode_reduced", spy)
    doc = Doc()
    ids = {}
    for k in range(4):
        strip = original.crop((0, 600 * k, 2400, 600 * (k + 1)))
        ids[f"S{k}"] = doc.image(jpeg(strip, 90), strip.size, filt=b"/DCTDecode", cs=b"/DeviceRGB")
    doc.page(b"\n".join(draw(n, (480, 0, 0, 120, 50, 700 - 120 * (k + 1))) for k, n in enumerate(ids)), ids)
    llm = Vision()
    r = extract(Path("Gross.pdf"), doc.build(), llm=llm)
    assert (r.status, r.incomplete) == ("ok", False) and len(llm.images) == 1
    assert loaded == [((600, 150), (600, 150))] * 4
    assert llm.images[0].size == (600, 600)
    assert similar(llm.images[0], original) < 12


def cmyk_plan(size) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    w, h = size
    d.rectangle([0, 0, w // 3, h // 2], fill=(200, 30, 30))
    d.rectangle([w // 3, 0, 2 * w // 3, h // 2], fill=(30, 160, 60))
    d.rectangle([2 * w // 3, h // 2, w - 1, h - 1], fill=(20, 40, 180))
    return img.convert("CMYK")


@pytest.mark.parametrize("kind", ["cmyk", "jpx"])
def test_reduced_decoding_reads_colours_as_pypdf_does(monkeypatch, kind):
    """CMYK JPEG (stored inverted) and JPEG 2000: decoded at reduced scale, the colours are those of the
    full decoding through pypdf."""
    if kind == "jpx" and not features.check("jpg_2000"):
        pytest.skip("Pillow ohne OpenJPEG")
    img = cmyk_plan((1200, 800)) if kind == "cmyk" else cmyk_plan((1200, 800)).convert("RGB")
    buf = io.BytesIO()
    if kind == "cmyk":
        img.save(buf, "JPEG", quality=95)
        filt, cs = b"/DCTDecode", b"/DeviceCMYK"
    else:
        img.save(buf, "JPEG2000", irreversible=False)
        filt, cs = b"/JPXDecode", b"/DeviceRGB"
    doc = Doc()
    doc.page(draw("Im1", (400, 0, 0, 266, 50, 50)), {"Im1": doc.image(buf.getvalue(), img.size, filt=filt, cs=cs)})
    data = doc.build()
    full = Vision()
    extract(Path("x.pdf"), data, llm=full)
    reduced_calls = []
    real = extract_mod._decode_reduced
    monkeypatch.setattr(extract_mod, "_decode_reduced", lambda s, t: reduced_calls.append(t) or real(s, t))
    monkeypatch.setattr(extract_mod, "MAX_IMAGE_PIXELS", 60_000)
    small = Vision()
    extract(Path("x.pdf"), data, llm=small)
    assert reduced_calls and full.images[0].size == (1200, 800) and small.images[0].size == (300, 200)
    reference = full.images[0].convert("RGB").resize((300, 200))
    diff = ImageChops.difference(reference, small.images[0].convert("RGB")).convert("L")
    assert sum(i * c for i, c in enumerate(diff.histogram())) / (300 * 200) < 6


# -- memory: measured in a child process -------------------------------------------------------------------------
# Peak RSS of the child's own address space (VmHWM): ru_maxrss would include the forking test process.
_MEASURE = r"""
import io, json, logging, sys
from pathlib import Path
logging.disable(logging.WARNING)
from projektakte.extract import extract

class LLM:
    vision = True
    model = "probe"
    sizes = []
    def describe_image(self, data, mime, prompt):
        from PIL import Image
        self.sizes.append(Image.open(io.BytesIO(data)).size)
        return type("R", (), {"text": "Plan gelesen"})()

llm = LLM()
r = extract(Path(sys.argv[1]), llm=llm)
peak = next(int(line.split()[1]) for line in open("/proc/self/status") if line.startswith("VmHWM:"))
print(json.dumps({"rss_mb": peak / 1024, "status": r.status, "incomplete": r.incomplete, "message": r.message,
                  "sizes": llm.sizes}))
"""
# An A0 plan scanned at 300 dpi in colour, written as a one-page PDF (in a child: the image takes 420 MB).
_A0_PLAN = r"""
import io, sys
from PIL import Image, ImageDraw
sys.path.insert(0, sys.argv[2])
from test_extract_r5 import Doc, draw, jpeg
plan = Image.new("RGB", (9933, 14043), "white")
d = ImageDraw.Draw(plan)
for y in range(100, 14000, 500):
    d.line([(0, y), (9933, y + 300)], fill=(0, 0, 200), width=8)
    d.text((100, y), "Brandschutzplan EG – Rettungsweg " * 10, fill=(200, 0, 0))
doc = Doc()
doc.page(draw("Im1", (2384, 0, 0, 3370, 0, 0)),
         {"Im1": doc.image(jpeg(plan, 80), plan.size, filt=b"/DCTDecode", cs=b"/DeviceRGB")})
open(sys.argv[1], "wb").write(doc.build())
"""


def _limit_memory():  # a regression fails with MemoryError instead of swapping the machine
    resource.setrlimit(resource.RLIMIT_AS, (3 * 1024 ** 3, 3 * 1024 ** 3))


def child(script: str, *args: str) -> str:
    proc = subprocess.run([sys.executable, "-c", script, *args], capture_output=True, text=True, cwd=ROOT,
                          env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"}, timeout=120,
                          preexec_fn=_limit_memory)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout


def measure(path: Path) -> dict:
    if not Path("/proc/self/status").exists():
        pytest.skip("VmHWM nur unter Linux")
    return json.loads(child(_MEASURE, str(path)).strip().splitlines()[-1])


def test_honest_a0_plan_in_colour_stays_well_below_500_mb(tmp_path):
    """An A0 plan scanned at 300 dpi in colour (9933×14043 JPEG, 139 MP): before, about 1.1 GB RSS; now the
    JPEG is decoded at half scale (35 MP) and sent at the full 16 MP."""
    path = tmp_path / "A0.pdf"
    child(_A0_PLAN, str(path), str(Path(__file__).parent))
    result = measure(path)
    assert (result["status"], result["incomplete"]) == ("ok", False)
    (w, h), = result["sizes"]
    assert 15_000_000 < w * h <= extract_mod.MAX_IMAGE_PIXELS  # sent at the full resolution allowed
    assert result["rss_mb"] < 400, result


def test_lying_jpeg_under_many_names_stays_small(tmp_path):
    """One 6000×6000 grey JPEG declared 1×1 under 50 names: before, one decoding per name (1.8 GB)."""
    doc = Doc()
    img = doc.image(jpeg(scan((6000, 6000)), 30), (6000, 6000), filt=b"/DCTDecode", declared=(1, 1))
    names = {f"X{k}": img for k in range(50)}
    doc.page(b"\n".join(draw(n, m) for n, m in zip(names, grid(50))), names)
    path = tmp_path / "Luege.pdf"
    path.write_bytes(doc.build())
    result = measure(path)
    assert (result["status"], result["incomplete"], result["message"]) == ("ok", False, None)
    assert len(result["sizes"]) == 1 and result["rss_mb"] < 250, result
