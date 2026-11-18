"""The model gets every PDF page as a viewer shows it (extract.page_images renders it), not its embedded images put
together: stencil masks are transparent where unset, /Rotate turns the page, text and drawings beside an image are
there, annotations are drawn and the crop box frames the page."""
from __future__ import annotations

import io
import zlib

from PIL import Image

from projektakte import extract as extract_mod


def _pdf(content: bytes, *, xobjects: dict[str, bytes] = (), extra: bytes = b"", annots: list[bytes] = ()) -> bytes:
    """One A4 page (595×842) with ``content``, image XObjects (name -> object body), Helvetica as /F1, page keys
    ``extra`` and annotations."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", b""]
    names = []
    for name, body in dict(xobjects).items():
        objects.append(body)
        names.append(b"/%s %d 0 R" % (name.encode(), len(objects)))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font = len(objects)
    objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    contents = len(objects)
    refs = []
    for annot in annots:
        objects.append(annot)
        refs.append(b"%d 0 R" % len(objects))
    objects[2] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources << /XObject << %s >> "
                  b"/Font << /F1 %d 0 R >> >> %s %s >>" % (contents, b" ".join(names), font, extra,
                                                           b"/Annots [%s]" % b" ".join(refs) if refs else b""))
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def _image(size, data: bytes, head: bytes) -> bytes:
    data = zlib.compress(data)
    return (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d %s /Filter /FlateDecode /Length %d >>\nstream\n"
            % (size[0], size[1], head, len(data)) + data + b"\nendstream")


def _grey_background() -> bytes:
    return _image((100, 140), bytes([120]) * 100 * 140, b"/ColorSpace /DeviceGray /BitsPerComponent 8")


def _rendered(data: bytes) -> Image.Image:
    (page,) = extract_mod.page_images(data, [1], "Seite.pdf")
    assert page.problem is None, page.problem
    mime, image = page.images[0]
    img = Image.open(io.BytesIO(image))
    img.load()
    assert mime == "image/jpeg" and img.mode == "RGB"
    return img


def _at(img: Image.Image, x_pt: float, y_pt: float) -> tuple[int, int, int]:
    """The pixel at a point of the (unrotated, uncropped A4) page, measured from its top left."""
    return img.getpixel((int(x_pt / 595 * img.width), int(y_pt / 842 * img.height)))


def test_a_stencil_mask_paints_only_where_it_is_set():
    """Seen: five /ImageMask images over a grey scan were set in as opaque black-and-white images – their white
    rectangles covered the page beneath. A stencil paints the fill colour where it is set, nothing elsewhere."""
    w, h = 80, 80
    rows = []
    for y in range(h):  # 1 bit per pixel, sample 1 = unset (Decode [0 1]); a black square in the middle
        bits = "".join("0" if 30 <= x < 50 and 30 <= y < 50 else "1" for x in range(w))
        rows.append(int(bits, 2).to_bytes(w // 8, "big"))
    mask = _image((w, h), b"".join(rows), b"/ImageMask true /BitsPerComponent 1")
    content = (b"q 595 0 0 842 0 0 cm /Bg Do Q\n"
               b"0 0 0 rg q 400 0 0 400 100 200 cm /Mk Do Q")
    img = _rendered(_pdf(content, xobjects={"Bg": _grey_background(), "Mk": mask}))
    assert abs(_at(img, 150, 300)[0] - 120) < 15  # the grey background shows through where the mask is unset
    assert _at(img, 297, 421)[0] < 40  # the set part in the fill colour (black)


def test_rotate_turns_the_page_as_a_viewer_does():
    content = b"q 595 0 0 842 0 0 cm /Bg Do Q"
    upright = _rendered(_pdf(content, xobjects={"Bg": _grey_background()}))
    turned = _rendered(_pdf(content, xobjects={"Bg": _grey_background()}, extra=b"/Rotate 90"))
    assert upright.width < upright.height and turned.width > turned.height


def test_text_and_drawings_beside_an_image_are_on_the_page_image():
    """Seen: a page with only a small logo as image went to the model as the logo alone; text and plan were lost."""
    logo = _image((20, 10), bytes([0]) * 200, b"/ColorSpace /DeviceGray /BitsPerComponent 8")
    content = (b"q 60 0 0 30 40 780 cm /Logo Do Q\n"
               b"BT /F1 40 Tf 60 400 Td (Rettungsweg) Tj ET\n"
               b"0 0 1 RG 6 w 60 200 m 500 200 l S")
    img = _rendered(_pdf(content, xobjects={"Logo": logo}))
    assert img.width < img.height and abs(img.width / img.height - 595 / 842) < 0.01  # the page, not the logo
    text = img.crop((int(60 / 595 * img.width), int(400 / 842 * img.height), int(300 / 595 * img.width),
                     int(442 / 842 * img.height)))
    assert min(text.convert("L").getdata()) < 80  # the text is there
    r, g, b = _at(img, 300, 842 - 200)
    assert b > 150 and r < 100 and g < 100  # the blue line is there, in colour


def test_annotations_are_drawn():
    stamp = (b"<< /Type /Annot /Subtype /Square /Rect [100 100 300 300] /C [1 0 0] /IC [1 0 0] "
             b"/Border [0 0 4] /F 4 >>")
    img = _rendered(_pdf(b"BT /F1 20 Tf 60 700 Td (Bescheid) Tj ET", annots=[stamp]))
    r, g, b = _at(img, 200, 842 - 200)
    assert r > 180 and g < 90 and b < 90


def test_the_crop_box_frames_the_page():
    img = _rendered(_pdf(b"BT /F1 20 Tf 60 700 Td (Bescheid) Tj ET", extra=b"/CropBox [0 421 595 842]"))
    assert img.width > img.height  # the upper half only, as a viewer shows it


def test_migration_reads_the_pdfs_the_model_read_once_more(tmp_path):
    import sqlite3

    from projektakte import db

    dbp = tmp_path / "alt.sqlite3"
    conn = sqlite3.connect(dbp)
    for i, script in enumerate(db.MIGRATIONS[:18], start=1):
        conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    rows = [(1, "gelesen.pdf", ".pdf", "vision"), (2, "text.pdf", ".pdf", "text"), (3, "wartet.pdf", ".pdf", "text"),
            (4, "cad.pdf", ".pdf", "text"), (5, "foto.jpg", ".jpg", "vision")]
    for doc_id, name, ext, method in rows:
        conn.execute("INSERT INTO documents(id, path, rel_path, filename, ext, status, method, extract_profile) "
                     "VALUES (?, ?, ?, ?, ?, 'ok', ?, 'v7:vision:m')", (doc_id, f"/x/{name}", name, name, ext, method))
    conn.execute("INSERT INTO page_texts(doc_id, page, state) VALUES (3, 1, 'done'), (3, 2, 'pending')")
    conn.execute("INSERT INTO page_texts(doc_id, page, state, note) VALUES (4, 1, 'done', ?)", (extract_mod.CAD_SKIPPED,))
    conn.commit()
    conn.close()
    db.init_db(dbp)
    with db.session(dbp) as conn:
        marked = conn.execute("SELECT id FROM documents WHERE incomplete = 1 AND extract_profile = '' ORDER BY id")
        assert [r[0] for r in marked] == [1, 3]  # photos go to the model as they are; CAD plans passed over stay
