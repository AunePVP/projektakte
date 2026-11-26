"""Test against the REAL page model on the Spark (Nemotron Parse with the type head) – skipped in the normal
suite; there is no fake of this service. Run it in the office network:

    PROJEKTAKTE_INTEGRATION=1 VISION_URL=http://<spark>:8767/v1 sh scripts/test.sh -m integration \
        tests/test_seitenmodell_real.py -n 0

(VISION_API_KEY if the service was started with --api-key.) The pages are invented demo text, rendered the way the
app renders them (extract.page_images at 175 dpi).
"""
import io
import os

import pytest

from projektakte import extract
from projektakte.ai.http import get_json
from projektakte.ai.seitenmodell import TYPES, PageRejected, SeitenmodellClient
from projektakte.demo.writers import pdf_bytes
from projektakte.seitentext import page_text, parse_blocks

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("PROJEKTAKTE_INTEGRATION") != "1" or not os.environ.get("VISION_URL"),
                       reason="nur mit PROJEKTAKTE_INTEGRATION=1 und VISION_URL gegen das echte Seitenmodell"),
]

PARAGRAPHS = [
    "Prüfbericht Brandschutz",
    "Die Rauchableitung im Treppenraum erfolgt über ein Fenster im obersten Geschoss.",
    "Die Löschwassermenge von 96 Kubikmetern je Stunde wird über einen Zeitraum von zwei Stunden vorgehalten.",
    "Der zweite Rettungsweg führt über die Außentreppe an der Nordseite des Gebäudes.",
]


@pytest.fixture(scope="module")
def client():
    return SeitenmodellClient(os.environ["VISION_URL"], os.environ.get("VISION_MODEL") or "nemotron-kmw",
                              timeout=float(os.environ.get("VISION_TIMEOUT") or 600),
                              api_key=os.environ.get("VISION_API_KEY", ""))


@pytest.fixture(scope="module")
def text_page():
    """A demo text page as the app sends it: rendered by PDFium at 175 dpi."""
    old = os.environ.get("VISION_DPI")
    os.environ["VISION_DPI"] = "175"
    try:
        pages = extract.page_images(pdf_bytes(PARAGRAPHS), [1], "pruefbericht.pdf")
    finally:
        if old is None:
            os.environ.pop("VISION_DPI", None)
        else:
            os.environ["VISION_DPI"] = old
    assert pages and pages[0].images, f"Seite nicht gezeichnet: {pages[0].problem if pages else 'keine Seite'}"
    return pages[0].images[0]  # (mime, bytes)


def white_page() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (1240, 1754), "white").save(out, "PNG")
    return out.getvalue()


def test_service_is_reachable(client):
    info = client.probe(30.0)
    assert info["ok"], info.get("error")
    assert info.get("model_listed"), f"Modell nicht in der Modellliste: {info.get('models_available')}"
    assert isinstance(info.get("dienst"), dict), info.get("dienst_error")


def test_health_of_the_service(client):
    base = client.url[:-3] if client.url.endswith("/v1") else client.url
    assert isinstance(get_json(f"{base}/health", timeout=30.0, headers=client._headers), dict)


def test_text_page_is_read(client, text_page):
    mime, data = text_page
    r = client.read_page(data, mime)
    assert r.typ == "Text", f"Typ {r.typ} (Kopf {r.kopf}, Sicherheit {r.sicherheit})"
    assert r.kopf in TYPES and r.sicherheit is not None and r.finish
    blocks = parse_blocks(r.raw)
    assert any(b.box is not None for b in blocks), "keine Blöcke mit Koordinaten im Rohtext"
    assert all(b.box is None or all(0.0 <= v <= 1.0 for v in b.box) for b in blocks)
    text = page_text(blocks)
    assert "<x_" not in text and "<class_" not in text
    for word in ("Rauchableitung", "Treppenraum", "Löschwassermenge", "Außentreppe"):
        assert word in text, f"{word!r} fehlt im gelesenen Text"
    assert not r.schleife


def test_white_page_is_empty(client):
    r = client.read_page(white_page(), "image/png")
    assert r.typ == "Leer", f"Typ {r.typ} (Kopf {r.kopf}, Sicherheit {r.sicherheit})"
    assert r.raw == ""


def test_broken_image_is_rejected(client):
    with pytest.raises(PageRejected):
        client.read_page(os.urandom(20), "image/png")
