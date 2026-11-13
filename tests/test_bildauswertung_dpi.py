"""VISION_DPI: the page images for the model are scaled to that resolution of the real page size (150 dpi by
default – measured: as good as 300 dpi with about a quarter of the image tokens); Tesseract keeps the full image."""
from __future__ import annotations

import io

from PIL import Image

from projektakte import extract as extract_mod
from projektakte.extract import MAX_IMAGE_PIXELS, for_model, page_images, vision_dpi, vision_pixels

from .test_extract_r5 import Doc, draw, jpeg, scan

A4 = (595, 842)  # points
A4_150 = (595 / 72 * 150) * (842 / 72 * 150)  # about 2.2 megapixels


def _size(data: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(data)).size


def test_pixels_follow_the_page_size(monkeypatch):
    monkeypatch.delenv("VISION_DPI", raising=False)
    assert vision_dpi() == 150 and abs(vision_pixels(*A4) - A4_150) < 2
    assert abs(vision_pixels(842, 1191) / A4_150 - 2) < 0.01  # A3: twice the pixels, the same dpi
    assert vision_pixels(2384 * 2, 3370 * 2) == MAX_IMAGE_PIXELS  # beyond A0: the general bound
    monkeypatch.setenv("VISION_DPI", "0")
    assert vision_pixels(*A4) is None
    monkeypatch.setenv("VISION_DPI", "abc")
    assert vision_dpi() == 150
    monkeypatch.setenv("VISION_DPI", "200")
    assert abs(vision_pixels(*A4) - A4_150 * (200 / 150) ** 2) < 3
    assert vision_pixels(0, 842) is None


def test_a_large_scan_is_scaled_a_small_one_stays():
    big = jpeg(scan((2480, 3508), text="Pruefbericht"))  # A4 at 300 dpi
    mime, out = for_model("image/jpeg", big, int(A4_150))
    w, h = _size(out)
    assert mime == "image/jpeg" and abs(w * h - A4_150) / A4_150 < 0.02 and abs(w / h - 2480 / 3508) < 0.01
    small = jpeg(scan((1200, 1700), text="klein"))
    assert for_model("image/jpeg", small, int(A4_150)) == ("image/jpeg", small)  # not worth re-encoding
    assert for_model("image/jpeg", big, None) == ("image/jpeg", big)
    assert for_model("image/png", b"kein Bild", 100) == ("image/png", b"kein Bild")


def test_a_bilevel_scan_is_scaled_in_grey():
    out = io.BytesIO()
    scan((2480, 3508), text="Schwarzweiss").convert("1").save(out, "PNG")
    mime, data = for_model("image/png", out.getvalue(), int(A4_150))
    img = Image.open(io.BytesIO(data))
    assert mime == "image/png" and img.mode == "L" and img.size[0] * img.size[1] <= A4_150 * 1.01


def test_the_phase_gets_the_page_at_vision_dpi(monkeypatch):
    monkeypatch.delenv("VISION_DPI", raising=False)
    doc = Doc()
    img = doc.image(jpeg(scan((2480, 3508), text="Seite")), (2480, 3508), filt=b"/DCTDecode")
    doc.page(draw("Im1", (595, 0, 0, 842, 0, 0)), {"Im1": img})
    pdf = doc.build()
    (page,) = page_images(pdf, [1], "Scan.pdf")
    w, h = _size(page.images[0][1])
    assert w * h <= A4_150 * 1.01 and w > 1000
    monkeypatch.setenv("VISION_DPI", "0")  # off: as before, up to MAX_IMAGE_PIXELS
    (page,) = page_images(pdf, [1], "Scan.pdf")
    assert _size(page.images[0][1]) == (2480, 3508)


def test_tesseract_keeps_the_full_image(monkeypatch):
    monkeypatch.delenv("VISION_DPI", raising=False)
    seen: list[tuple[int, int]] = []

    def fake(data: bytes) -> str:
        seen.append(_size(data))
        return "Seite gescannt Rauchabzug Treppenraum erkannt"

    monkeypatch.setattr(extract_mod, "ocr_available", lambda: True)
    monkeypatch.setattr(extract_mod, "ocr_image", fake)

    class Model:
        vision, model = True, "bildmodell"
        sizes: list = []

        def describe_image(self, data, mime, prompt):
            self.sizes.append(_size(data))
            return type("R", (), {"text": "Rauchabzug Treppenraum geprueft"})()

    doc = Doc()
    img = doc.image(jpeg(scan((2480, 3508), text="Seite")), (2480, 3508), filt=b"/DCTDecode")
    doc.page(draw("Im1", (595, 0, 0, 842, 0, 0)), {"Im1": img})
    llm = Model()
    extract_mod.extract("Scan.pdf", doc.build(), llm=llm)
    assert seen == [(2480, 3508)]
    (w, h), = llm.sizes
    assert w * h <= A4_150 * 1.01
