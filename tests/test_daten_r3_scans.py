"""Round 3: scan pages with many images (strip and tile scans) are put together into one page image;
pages read only partly – an image not decodable, a model call failed, images left out – are marked
incomplete instead of counting as fully read. Mail bodies without a charset keep their umlauts."""
import io
import math
import zlib
from email.message import EmailMessage
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

from projektakte import db
from projektakte.documents import index_all_documents
from projektakte.extract import MAX_IMAGES_PER_PAGE, extract, mime_text
from projektakte.mails.model import parse_eml
from projektakte.registry import scan_projects, sync_projects


# -- PDF builder with free placement ------------------------------------------------------------------
def jpeg(img: Image.Image) -> dict:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=95)
    return {"data": buf.getvalue(), "filter": "/DCTDecode", "size": img.size, "cs": "/DeviceRGB", "bpc": 8}


def grey(img: Image.Image) -> dict:
    g = img.convert("L")
    return {"data": zlib.compress(g.tobytes()), "filter": "/FlateDecode", "size": g.size, "cs": "/DeviceGray",
            "bpc": 8}


def broken() -> dict:
    return {"data": b"\xff\xd8 kein echtes JPEG", "filter": "/DCTDecode", "size": (100, 100), "cs": "/DeviceRGB",
            "bpc": 8}


def pdf(pages: list[dict]) -> bytes:
    """Each page: {"images": [(xobject, matrix | None)], "unused": [xobject], "form": bool}.
    matrix None = listed in the resources but never drawn. With "form", the images are drawn inside a
    form XObject that is itself moved by a /Matrix."""
    objects: list[bytes] = []

    def add(obj: bytes) -> int:
        objects.append(obj)
        return len(objects)

    def stream(head: bytes, data: bytes) -> bytes:
        return b"<< " + head + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"

    def image(x: dict) -> int:
        head = (b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace %s /BitsPerComponent %d /Filter %s"
                % (x["size"][0], x["size"][1], x["cs"].encode(), x["bpc"], x["filter"].encode()))
        return add(stream(head, x["data"]))

    parts = []
    for page in pages:
        names, ops = [], []
        for n, (x, matrix) in enumerate(page["images"], start=1):
            names.append(b"/Im%d %d 0 R" % (n, image(x)))
            if matrix is not None:
                ops.append(b"q %s cm /Im%d Do Q" % (" ".join(f"{v:g}" for v in matrix).encode(), n))
        res = b"<< /XObject << " + b" ".join(names) + b" >> >>"
        if page.get("form"):  # the images live in a form moved 10 pt to the right
            form = add(stream(b"/Type /XObject /Subtype /Form /BBox [0 0 2000 2000] /Matrix [1 0 0 1 10 0] "
                              b"/Resources " + res, b"\n".join(ops)))
            res = b"<< /XObject << /Fm1 %d 0 R >> >>" % form
            ops = [b"q /Fm1 Do Q"]
        content = add(stream(b"", b"\n".join(ops)))
        parts.append((content, res))
    pages_id = len(objects) + len(parts) + 1
    kids = [add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 842] /Contents %d 0 R /Resources %s >>"
                % (pages_id, content, res)) for content, res in parts]
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>"
               % (b" ".join(b"%d 0 R" % k for k in kids), len(kids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    return bytes(out)


def page_scan(size=(480, 640)) -> Image.Image:
    """A scanned page: text lines, a red frame and a black marker in the top-left corner."""
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    for y in range(60, size[1] - 40, 40):
        d.text((40, y), f"Auflage {y // 40}: Rauchabzug im Treppenraum", fill="black")
    d.rectangle([4, 4, size[0] - 5, size[1] - 5], outline=(200, 0, 0), width=4)
    d.rectangle([12, 12, 40, 40], fill="black")
    return img


def strips(img: Image.Image, n: int, *, top=800.0, left=50.0, scale=0.75):
    """Cut a page into ``n`` horizontal strips, placed one below the other as a scanner does."""
    w, h = img.size
    step = h // n
    out = []
    for k in range(n):
        strip = img.crop((0, k * step, w, (k + 1) * step))
        y = top - (k + 1) * step * scale
        out.append((strip, (w * scale, 0, 0, step * scale, left, y)))
    return out


class Vision:
    vision = True
    model = "vision-r3"

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.images: list[Image.Image] = []

    def describe_image(self, data, mime, prompt):
        img = Image.open(io.BytesIO(data))
        img.load()
        assert mime == {"JPEG": "image/jpeg", "PNG": "image/png"}[img.format]
        self.images.append(img)
        answer = self.answers.pop(0) if self.answers else f"Transkript {len(self.images)}: Rauchabzug"
        if isinstance(answer, Exception):
            raise answer

        class Result:
            text = answer
        return Result()


def similar(a: Image.Image, b: Image.Image) -> float:
    """Mean absolute pixel difference (0 … 255) after scaling ``a`` to ``b``'s size."""
    a = a.convert("RGB").resize(b.size)
    diff = ImageChops.difference(a, b.convert("RGB")).convert("L")
    return sum(i * c for i, c in enumerate(diff.histogram())) / (b.size[0] * b.size[1])


# -- strip and tile scans -----------------------------------------------------------------------------
def test_strip_scan_is_put_together_into_one_page_image():
    original = page_scan()
    data = pdf([{"images": [(jpeg(s), m) for s, m in strips(original, 8)]}])
    llm = Vision()
    r = extract(Path("Streifen.pdf"), data, llm=llm)
    assert (r.status, r.method, r.incomplete, r.retry, r.message) == ("ok", "vision", False, False, None)
    assert len(llm.images) == 1  # one call for the whole page, not 3 of 8 strips
    sent = llm.images[0]
    assert abs(sent.size[0] / sent.size[1] - 480 / 640) < 0.02
    assert sent.size[0] >= 470  # native resolution of the strips kept
    assert similar(sent, original) < 6  # the page as scanned, strips in the right order
    assert sent.convert("L").getpixel((20, 20)) < 60  # marker top left: not upside down


def test_tiles_of_different_formats_and_an_image_inside_a_form():
    original = page_scan((400, 400))
    tiles = []
    for row in range(2):
        for col in range(2):
            tile = original.crop((col * 200, row * 200, (col + 1) * 200, (row + 1) * 200))
            x = jpeg(tile) if (row + col) % 2 == 0 else grey(tile)
            tiles.append((x, (100, 0, 0, 100, 100 + col * 100, 600 - (row + 1) * 100)))
    tiles.append((jpeg(original.crop((0, 0, 50, 50))), (5, 0, 0, 5, 100, 600)))  # tiny logo above
    llm = Vision()
    r = extract(Path("Kacheln.pdf"), pdf([{"images": tiles, "form": True}]), llm=llm)
    assert (r.status, r.incomplete, r.message) == ("ok", False, None)
    assert len(llm.images) == 1
    sent = llm.images[0].convert("RGB")
    w, h = sent.size
    body = sent.crop((0, h - w, w, h))  # the 2×2 tiles below the logo strip
    assert similar(body, original.convert("L").convert("RGB")) < 25


def test_rotated_placement_is_turned_back():
    original = page_scan((300, 200))
    # image turned by 90° on the page (landscape scan in a portrait page): columns run up the page
    turned = (0, 300, -200, 0, 400, 100)
    llm = Vision()
    images = [(jpeg(original), turned)] + [(jpeg(page_scan((60, 60))), (30, 0, 0, 30, 450 + 40 * k, 700))
                                           for k in range(3)]
    r = extract(Path("Quer.pdf"), pdf([{"images": images}]), llm=llm)
    assert r.status == "ok" and len(llm.images) == 1
    sent = llm.images[0].convert("RGB")
    # the big image fills the left part of the page image: turned counter-clockwise like on the page
    expected = original.transpose(Image.Transpose.ROTATE_90)
    scale = sent.size[0] / 360  # page image spans x 200 … 560 pt and y 100 … 730 pt
    assert abs(sent.size[1] / scale - 630) < 2
    box = (0, round((730 - 400) * scale), round(200 * scale), round((730 - 100) * scale))
    assert similar(sent.crop(box), expected) < 12


def test_images_that_cannot_be_put_together_leave_the_page_incomplete():
    skew = (100 * math.cos(0.5), 100 * math.sin(0.5), -100 * math.sin(0.5), 100 * math.cos(0.5), 300, 300)
    images = [(jpeg(page_scan((200 + 20 * k, 200))), (100, 0, 0, 100, 50, 50 + 110 * k)) for k in range(5)]
    images.append((jpeg(page_scan((100, 100))), skew))
    llm = Vision()
    r = extract(Path("Schief.pdf"), pdf([{"images": images}]), llm=llm)
    assert (r.status, r.incomplete, r.retry) == ("ok", True, False)
    assert len(llm.images) == MAX_IMAGES_PER_PAGE
    assert sorted(i.size[0] for i in llm.images) == [240, 260, 280]  # the largest ones
    assert r.message == ("1 Seite(n) nur teilweise gelesen (3 Bild(er) nicht ausgewertet (mehr als 3 Bilder je "
                         "Seite, nicht zu einem Seitenbild zusammensetzbar))")


# -- partly read pages --------------------------------------------------------------------------------
def test_page_with_one_unreadable_image_is_incomplete_and_retried():
    llm = Vision()
    r = extract(Path("x.pdf"), pdf([{"images": [(broken(), (200, 0, 0, 200, 50, 50)),
                                                  (grey(page_scan((200, 200))), (200, 0, 0, 200, 300, 50))]}]),
                llm=llm)
    assert (r.status, r.method, r.incomplete, r.retry) == ("ok", "vision", True, True)
    assert len(llm.images) == 1 and "Rauchabzug" in r.text
    assert r.message == ("1 Seite(n) nur teilweise gelesen (1 Bild(er) nicht lesbar: Bilddaten beschädigt oder "
                         "Format unbekannt)")


def test_strip_scan_with_a_broken_strip_is_put_together_but_incomplete():
    parts = strips(page_scan(), 5)
    images = [(broken() if k == 2 else jpeg(s), m) for k, (s, m) in enumerate(parts)]
    llm = Vision()
    r = extract(Path("x.pdf"), pdf([{"images": images}]), llm=llm)
    assert len(llm.images) == 1
    assert (r.status, r.incomplete, r.retry) == ("ok", True, True)
    assert "1 Bild(er) nicht lesbar" in r.message


def test_failed_model_call_for_one_image_is_incomplete_and_retried():
    llm = Vision(["Grundriss", RuntimeError("timeout")])
    r = extract(Path("x.pdf"), pdf([{"images": [(jpeg(page_scan((200, 200))), (200, 0, 0, 200, 50, 50)),
                                                  (jpeg(page_scan((200, 200))), (200, 0, 0, 200, 300, 50))]}]),
                llm=llm)
    assert (r.status, r.incomplete, r.retry) == ("ok", True, True) and r.text == "Grundriss"
    assert r.message == "1 Seite(n) nur teilweise gelesen (1 Bild(er): Bildauswertung fehlgeschlagen)"


def test_an_empty_answer_for_a_logo_is_no_gap():
    llm = Vision(["Grundriss", ""])
    r = extract(Path("x.pdf"), pdf([{"images": [(jpeg(page_scan((300, 300))), (300, 0, 0, 300, 50, 50)),
                                                  (jpeg(page_scan((40, 40))), (20, 0, 0, 20, 500, 800))]}]),
                llm=llm)
    assert (r.status, r.incomplete, r.retry, r.message) == ("ok", False, False, None)


def test_only_images_drawn_on_the_page_are_read():
    """Some PDF tools give every page the resources of all pages: unused images are no content."""
    drawn = page_scan((300, 300))
    unused = [(jpeg(page_scan((500, 500))), None) for _ in range(4)]
    llm = Vision()
    r = extract(Path("x.pdf"), pdf([{"images": unused + [(jpeg(drawn), (300, 0, 0, 300, 50, 50))]}]), llm=llm)
    assert (r.status, r.incomplete) == ("ok", False)
    assert [i.size for i in llm.images] == [(300, 300)]


def test_partly_read_scan_is_stored_incomplete_with_retry_time(tmp_path):
    root = tmp_path / "projekte"
    proj = root / "BS-2024-0001 Testhalle"
    proj.mkdir(parents=True)
    images = [(broken(), (200, 0, 0, 200, 50, 50)), (jpeg(page_scan((200, 200))), (200, 0, 0, 200, 300, 50))]
    (proj / "Teilweise.pdf").write_bytes(pdf([{"images": images}]))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_projects(conn, scan_projects(root, r"BS-\d{4}-\d{4}"))
    conn.commit()
    index_all_documents(conn, llm=Vision(), projects_dir=root)
    row = conn.execute("SELECT status, incomplete, attempts, retry_after, message FROM documents").fetchone()
    assert (row["status"], row["incomplete"], row["attempts"]) == ("ok", 1, 1)
    assert row["retry_after"] and "nur teilweise gelesen" in row["message"]
    conn.close()


# -- mail body without charset ------------------------------------------------------------------------
def raw_mail(body: bytes, ctype: str = "text/plain", cte: str = "8bit") -> bytes:
    head = (b"From: a@b.example\r\nTo: c@d.example\r\nSubject: Pruefbericht\r\n"
            b"Date: Tue, 04 Nov 2025 09:00:00 +0100\r\nMessage-ID: <cs@b.example>\r\nMIME-Version: 1.0\r\n")
    return (head + b"Content-Type: " + ctype.encode() + b"\r\nContent-Transfer-Encoding: " + cte.encode()
            + b"\r\n\r\n" + body)


TEXT = "Prüfbericht Brandschutztür: Rauchschutztür selbstschließend, Größe 1,01 m × 2,13 m"


def test_mail_body_without_charset_keeps_umlauts():
    for data in (TEXT.encode("utf-8"), TEXT.replace("×", "x").encode("cp1252")):
        rec = parse_eml(raw_mail(data), "eml:x.eml")
        assert "Prüfbericht Brandschutztür" in rec.body and "selbstschließend" in rec.body, rec.body
        assert "�" not in rec.body
    html = parse_eml(raw_mail(b"<p>" + TEXT.encode("utf-8") + b"</p>", "text/html"), "eml:y.eml")
    assert "Größe" in html.body
    # "us-ascii" declared for 8-bit text (common in old mail programs) – the same
    ascii_declared = parse_eml(raw_mail(TEXT.encode("utf-8"), 'text/plain; charset="us-ascii"'), "eml:z.eml")
    assert "Prüfbericht" in ascii_declared.body
    # base64 without charset
    import base64
    b64 = parse_eml(raw_mail(base64.encodebytes(TEXT.encode("utf-8")), cte="base64"), "eml:b.eml")
    assert "Größe" in b64.body
    # project folder .eml files are extracted the same way
    assert "Prüfbericht Brandschutztür" in extract(Path("Mail.eml"), raw_mail(TEXT.encode("utf-8"))).text


def test_declared_charsets_are_still_used():
    msg = EmailMessage()
    msg.set_content(TEXT.replace("×", "x"), charset="iso-8859-15")
    assert mime_text(msg).strip() == TEXT.replace("×", "x")
    msg = EmailMessage()
    msg.set_content(TEXT, charset="utf-8")
    assert mime_text(msg).strip() == TEXT
    # a wrong/unknown charset falls back to UTF-8, then Windows-1252
    bad = raw_mail(TEXT.encode("utf-8"), 'text/plain; charset="x-kaputt"')
    assert "Prüfbericht" in parse_eml(bad, "eml:k.eml").body
    # pure ASCII is unaffected
    assert parse_eml(raw_mail(b"Termin am Dienstag"), "eml:a.eml").body == "Termin am Dienstag"


def test_mails_imported_with_lost_umlauts_are_read_again(tmp_path):
    from projektakte.assign import assign_mails, decide
    from projektakte.mails.eml_source import sync_eml_folder

    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    conn.execute("INSERT INTO projects(number, name, path) VALUES('BS-2024-0001', 'Testhalle', ?)",
                 (str(tmp_path / "p"),))
    conn.commit()
    mail_dir, cache = tmp_path / "mails", tmp_path / "cache"
    mail_dir.mkdir()
    (mail_dir / "charset.eml").write_bytes(raw_mail(TEXT.encode("utf-8")))
    sync_eml_folder(conn, mail_dir, cache_dir=cache)
    assign_mails(conn, r"BS-\d{4}-\d{4}", min_share=0.5, min_lead=0.2, vector_key="k")
    mail_id = conn.execute("SELECT id FROM mails").fetchone()[0]
    decide(conn, mail_id, "assign", "BS-2024-0001", user="anna")
    # the state an older version left: body and hash from ASCII decoding
    conn.execute("UPDATE mails SET body = replace(body, 'ü', char(65533, 65533)), sha256 = 'alt'")
    conn.execute("DELETE FROM meta WHERE key = 'mail_body_charset'")
    conn.commit()
    assert sync_eml_folder(conn, mail_dir, cache_dir=cache)["changed"] == 1
    body = conn.execute("SELECT body FROM mails WHERE id = ?", (mail_id,)).fetchone()[0]
    assert "Prüfbericht Brandschutztür" in body
    assert conn.execute("SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH 'prufbericht'").fetchone()[0] == 1
    assign_mails(conn, r"BS-\d{4}-\d{4}", min_share=0.5, min_lead=0.2, vector_key="k")
    assert tuple(conn.execute("SELECT project, assign_status FROM mails").fetchone()) == ("BS-2024-0001", "confirmed")
    assert sync_eml_folder(conn, mail_dir, cache_dir=cache)["unchanged"] == 1  # once only
    conn.close()


def test_eml_documents_with_lost_umlauts_are_extracted_again(tmp_path):
    root = tmp_path / "projekte"
    proj = root / "BS-2024-0001 Testhalle"
    proj.mkdir(parents=True)
    (proj / "Pruefbericht.eml").write_bytes(raw_mail(TEXT.encode("utf-8")))
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    sync_projects(conn, scan_projects(root, r"BS-\d{4}-\d{4}"))
    conn.commit()
    index_all_documents(conn, projects_dir=root)
    conn.execute("UPDATE chunks SET text = replace(text, 'ü', char(65533, 65533))")
    conn.execute("DELETE FROM meta WHERE key = 'eml_text_charset'")
    conn.commit()
    assert index_all_documents(conn, projects_dir=root)["reprocessed"] == 1
    text = conn.execute("SELECT text FROM chunks").fetchone()[0]
    assert "Prüfbericht Brandschutztür" in text and "�" not in text
    assert index_all_documents(conn, projects_dir=root)["unchanged"] == 1
    conn.close()
