"""VISION_SKIP_FOLDERS: photos in folders such as 263900-Bilder are not sent to the vision model; removing
the setting later reads them. Scans elsewhere are read as before."""
from __future__ import annotations

import io

from PIL import Image

from projektakte import db
from projektakte.config import load_settings
from projektakte.documents import PHOTO_SKIPPED_TEXT, _in_folder, index_all_documents
from projektakte.registry import sync_from_folder


class Vision:
    vision, model = True, "bildmodell"

    def __init__(self):
        self.calls = 0

    def describe_image(self, data, mime, prompt):
        self.calls += 1
        return type("R", (), {"text": "Brandschutztür T30 mit Feststellanlage."})()


def _jpg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(out, "JPEG")
    return out.getvalue()


def _run(conn, root, llm, skip):
    sync_from_folder(conn, root, r"\d{6}", max_depth=4)
    conn.commit()
    index_all_documents(conn, llm, projects_dir=root, skip_photos=skip)
    return {r[0]: (r[1], r[2]) for r in conn.execute("SELECT rel_path, status, message FROM documents")}


def test_photos_in_picture_folders_wait_until_the_setting_is_removed(tmp_path):
    root = tmp_path / "projekt"
    order = root / "2/26/2639_Bahnhof/263900_Konzept"
    (order / "263900-Bilder").mkdir(parents=True)
    (order / "263900-Text").mkdir()
    (order / "263900-Bilder/Foto.jpg").write_bytes(_jpg())
    (order / "263900-Text/Skizze.jpg").write_bytes(_jpg())
    dbp = tmp_path / "x.sqlite3"
    db.init_db(dbp)
    conn = db.connect(dbp)
    llm = Vision()
    docs = _run(conn, root, llm, ("Bilder",))
    assert docs["263900-Bilder/Foto.jpg"] == ("skipped", PHOTO_SKIPPED_TEXT)
    assert docs["263900-Text/Skizze.jpg"][0] == "ok" and llm.calls == 1
    _run(conn, root, llm, ("Bilder",))
    assert llm.calls == 1  # nothing read again
    docs = _run(conn, root, llm, ())  # the option removed: the photo is read now
    assert docs["263900-Bilder/Foto.jpg"][0] == "ok" and llm.calls == 2
    conn.close()


def test_folder_rule():
    assert _in_folder("263900-Bilder/a.jpg", "263900_Konzept", ("Bilder",))
    assert _in_folder("Unterordner/b.jpg", "119300-bilder", ("Bilder",))  # the project folder itself
    assert _in_folder("263900_Bilder/2020/c.jpg", "x", ("bilder",))
    assert not _in_folder("Bildergalerie/d.jpg", "x", ("Bilder",))
    assert not _in_folder("263900-Text/e.jpg", "x", ("Bilder",))


def test_setting():
    assert load_settings({"VISION_SKIP_FOLDERS": "Bilder, Fotos"}).vision_skip_folders == ("Bilder", "Fotos")
    assert load_settings({}).vision_skip_folders == ()
