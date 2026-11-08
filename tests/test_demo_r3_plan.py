"""Round 3 (DEMO-12-Rest): the demo escape plan follows the usual sign conventions.

ISO 7010 E002 emergency exit (running man, door, arrow – no "EXIT"), E007 assembly point (not the
first-aid cross), F001 fire extinguisher in red, an own "you are here" symbol, a legend, no
overlapping labels, deterministic pixels, and labels that match the fake vision transcription.
"""
import base64
import hashlib
import re
import struct
import zlib

import pytest

from projektakte.demo import DEMO_VERSION, generate
from projektakte.demo.data import PROJECTS
from projektakte.demo.writers import (_BLUE, _GREEN, _RED, _WHITE, PLAN_SIZE, _Canvas, _draw_plan,
                                      plan_layout, plan_png)
from projektakte.fake_ai.core import chat_completion

SUBTITLE = "BS-2024-0137 Grundschule Am Lindenhof"
LEGEND_TOP = 372  # everything below belongs to the legend


def _pixels(png: bytes) -> tuple[int, int, bytes]:
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, color = struct.unpack(">IIBB", png[16:26])
    assert depth == 8 and color == 2  # 8-bit RGB
    raw = zlib.decompress(png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8])
    stride = 1 + 3 * width
    assert all(raw[y * stride] == 0 for y in range(height))  # filter type 0 everywhere
    return width, height, b"".join(raw[y * stride + 1:(y + 1) * stride] for y in range(height))


@pytest.fixture(scope="module")
def plan():
    png = plan_png(subtitle=SUBTITLE)
    width, height, rgb = _pixels(png)
    return {"png": png, "w": width, "h": height, "rgb": rgb, "items": plan_layout(subtitle=SUBTITLE)}


def _px(plan, x: int, y: int) -> tuple[int, int, int]:
    i = 3 * (y * plan["w"] + x)
    return tuple(plan["rgb"][i:i + 3])


def _boxes(plan, kind: str, name: str | None = None, legend: bool | None = None) -> list[tuple]:
    out = []
    for k, n, box in plan["items"]:
        if k == kind and (name is None or n == name) and (
                legend is None or (box[1] >= LEGEND_TOP) == legend):
            out.append(box)
    return out


def _overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _sample(plan, box: tuple, u: int, v: int) -> tuple[int, int, int]:
    """Pixel at sign units (u, v); 100 units = the sign's height."""
    size = box[3] - box[1]
    return _px(plan, box[0] + u * size // 100, box[1] + v * size // 100)


def test_plan_is_deterministic_and_version_pinned(plan):
    again = _draw_plan("FLUCHTWEGPLAN ERDGESCHOSS", SUBTITLE).png()
    assert again == plan["png"]  # same bytes on every run (no cache involved)
    assert (plan["w"], plan["h"]) == PLAN_SIZE and 600 <= plan["w"] <= 1200 and 400 <= plan["h"] <= 800
    assert len(plan["png"]) < 40_000
    # the pixels are pinned (zlib output may differ between zlib builds, pixels never do):
    # when the drawing changes on purpose, bump DEMO_VERSION in demo/generate.py and update this.
    digest = hashlib.sha256(plan["rgb"]).hexdigest()[:16]
    assert (DEMO_VERSION, digest) == ("5", "516860a6f9ac89a0"), digest


def test_generated_demo_plan_is_this_drawing(tmp_path, plan):
    generate(tmp_path)
    png = (tmp_path / "projekte/BS-2024-0137 Grundschule Am Lindenhof/05 Pläne/Fluchtwegplan EG.png")
    assert png.read_bytes() == plan["png"]


def test_labels_and_signs_do_not_overlap(plan):
    items = [(k, n, b) for k, n, b in plan["items"] if k != "area"]
    w, h = PLAN_SIZE
    for kind, name, box in items:
        assert 0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h, (kind, name, box)
    free = [(i, it) for i, it in enumerate(items) if it[0] in ("text", "sign")]
    for i, (kind, name, box) in free:
        for j, (k2, n2, b2) in enumerate(items):
            if i != j and _overlap(box, b2):
                pytest.fail(f"{kind} {name!r} {box} overlaps {k2} {n2!r} {b2}")


def test_no_exit_text_but_german_labels_and_a_legend(plan):
    texts = [n for k, n, _ in plan["items"] if k == "text"]
    assert not any("EXIT" in t for t in texts)
    for label in ("FLUCHTWEGPLAN ERDGESCHOSS", "NOTAUSGANG OST", "SAMMELPLATZ", "PARKPLATZ NORD",
                  "SIE SIND HIER", "TREPPENRAUM 1", "LEGENDE", SUBTITLE.upper()):
        assert label in texts, label
    legend = {n for k, n, b in plan["items"] if k == "text" and b[1] >= LEGEND_TOP}
    assert {"FLUCHTWEG", "NOTAUSGANG", "SAMMELPLATZ", "FEUERLÖSCHER", "STANDORT"} <= legend
    # every symbol used on the plan is explained in the legend
    for name in ("notausgang", "sammelplatz", "feuerloescher", "standort"):
        assert _boxes(plan, "sign", name, legend=False) and _boxes(plan, "sign", name, legend=True), name
    assert _boxes(plan, "arrow", "fluchtweg", legend=False) and _boxes(plan, "arrow", legend=True)


def test_colours_have_one_meaning_each(plan):
    """Red = fire extinguisher only (the old plan used a red square also for "you are here"),
    blue = "you are here" (symbol and its label), safety green = rescue signs and escape arrows."""
    red = _boxes(plan, "sign", "feuerloescher")
    blue = _boxes(plan, "sign", "standort") + [b for k, n, b in plan["items"] if n == "SIE SIND HIER"]
    green = _boxes(plan, "sign", "notausgang") + _boxes(plan, "sign", "sammelplatz") + _boxes(plan, "arrow")
    stray = {"red": 0, "blue": 0, "green": 0}
    counts = {"red": 0, "blue": 0, "green": 0}
    for key, colour, boxes in (("red", _RED, red), ("blue", _BLUE, blue), ("green", _GREEN, green)):
        for m in re.finditer(b"(?=" + re.escape(bytes(colour)) + b")", plan["rgb"]):
            if m.start() % 3:
                continue  # not aligned to a pixel
            y, x = divmod(m.start() // 3, plan["w"])
            counts[key] += 1
            if not any(b[0] <= x < b[2] and b[1] <= y < b[3] for b in boxes):
                stray[key] += 1
    assert stray == {"red": 0, "blue": 0, "green": 0}
    assert counts["red"] > 300 and counts["blue"] > 300 and counts["green"] > 2000


def test_rescue_signs_are_pictograms(plan):
    for box in _boxes(plan, "sign", "notausgang") + _boxes(plan, "sign", "sammelplatz"):
        colours = {_px(plan, x, y) for y in range(box[1], box[3]) for x in range(box[0], box[2])}
        assert colours == {_GREEN, _WHITE}, box  # white pictogram on green, nothing else
    for box in _boxes(plan, "sign", "feuerloescher"):
        colours = {_px(plan, x, y) for y in range(box[1], box[3]) for x in range(box[0], box[2])}
        assert colours == {_RED, _WHITE}, box
    exit_sign = _boxes(plan, "sign", "notausgang", legend=False)[0]
    assert exit_sign[2] - exit_sign[0] == 2 * (exit_sign[3] - exit_sign[1])  # pictogram + arrow
    assert _sample(plan, exit_sign, 46, 17) == _WHITE  # head of the running man
    assert _sample(plan, exit_sign, 62, 50) == _WHITE  # door frame
    assert _sample(plan, exit_sign, 77, 50) == _GREEN  # inside the door
    assert _sample(plan, exit_sign, 135, 50) == _WHITE  # direction arrow …
    assert _sample(plan, exit_sign, 135, 30) == _GREEN  # … not a filled block of text
    assert _sample(plan, exit_sign, 195, 8) == _GREEN
    for sign in _boxes(plan, "sign", "sammelplatz"):  # E007, not the first-aid cross E003
        for u, v in ((13, 13), (87, 13), (13, 87), (87, 87)):
            assert _sample(plan, sign, u, v) == _WHITE, (sign, u, v)  # arrows from the corners
        assert _sample(plan, sign, 50, 24) == _GREEN  # a cross would have its bar here
        assert _sample(plan, sign, 50, 60) == _WHITE  # people in the middle
        assert _sample(plan, sign, 50, 92) == _GREEN


def test_layout_matches_the_fake_vision_transcription(plan):
    payload = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "Lies den Plan"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                                            + base64.b64encode(plan["png"]).decode()}}]}]}
    text = chat_completion(payload, think=False)["choices"][0]["message"]["content"]
    transcription = text.split(":", 1)[1]
    label_words = {w for k, n, _ in plan["items"] if k == "text" for w in n.split()}
    words = {w.upper() for w in re.findall(r"[\wÄÖÜäöüß]+", transcription)} - {"NEBEN"}
    assert words and words <= label_words, words - label_words
    # … and the geometry fits the words: exit in the east, assembly point to the north,
    # the extinguisher next to staircase 1
    walls = _boxes(plan, "wall", legend=False)
    east = max(b[2] for b in walls)
    exit_sign = _boxes(plan, "sign", "notausgang", legend=False)[0]
    assembly = _boxes(plan, "sign", "sammelplatz", legend=False)[0]
    assert exit_sign[0] >= east and assembly[0] >= east
    assert assembly[3] < exit_sign[1]
    staircase = next(b for k, n, b in plan["items"] if k == "text" and n == "TREPPENRAUM 1")
    extinguisher = _boxes(plan, "sign", "feuerloescher", legend=False)[0]
    assert extinguisher[0] < staircase[2] and extinguisher[1] - staircase[3] < 40


@pytest.mark.parametrize("project", PROJECTS, ids=lambda p: p["number"])
def test_every_demo_project_name_fits_the_plan(project):
    items = plan_layout(subtitle=f"{project['number']} {project['name']}")
    sub = next(b for k, n, b in items if k == "text" and n.startswith(project["number"]))
    assert sub[2] <= PLAN_SIZE[0]


def test_unknown_glyph_is_an_error_not_a_gap():
    c = _Canvas(100, 20)
    c.text(0, 0, "Prüfung Ä/Ö/Ü 0-9")  # umlauts and digits exist
    with pytest.raises(ValueError):
        c.text(0, 0, "Café")
