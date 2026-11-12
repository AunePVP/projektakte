"""Project overview views (list / tiles / columns), chosen per person on /einstellungen."""
from fastapi.testclient import TestClient

from projektakte import db
from projektakte.main import create_app

from .conftest import fake_settings


def _client(tmp_path, fake_ai):
    s = fake_settings(fake_ai, tmp_path)
    app = create_app(s, background=False)
    with db.session(s.db_path) as conn:
        for number, name, parent, kind in (
                ("2095", "Rathaus", None, "bauvorhaben"), ("209500", "Rathaus Brandschutz", "2095", "projekt"),
                ("209501", "Rathaus Mängel", "2095", "projekt"), ("2001", "Schule", None, "bauvorhaben"),
                ("200100", "Schule Konzept", "2001", "projekt"), ("3100", "Halle", None, "bauvorhaben"),
                ("310000", "Halle Konzept", "3100", "projekt")):
            conn.execute("INSERT INTO projects(number, name, path, parent, kind) VALUES(?,?,?,?,?)",
                         (number, name, f"/x/{number}", parent, kind))
    return TestClient(app), s


def test_default_is_the_list(tmp_path, fake_ai):
    c, _ = _client(tmp_path, fake_ai)
    assert "Rathaus" in c.get("/").text and "Schule" in c.get("/").text


def test_tiles_show_folders_first_and_count_only_projects(tmp_path, fake_ai):
    c, _ = _client(tmp_path, fake_ai)
    assert c.post("/einstellungen", data={"ansicht": "kacheln"}, follow_redirects=False).status_code == 303
    top = c.get("/").text
    assert "Rathaus" not in top and "2 Projekte" in top and "1 Projekt<" in top  # folder 2: sites, not orders
    sub = c.get("/", params={"o": "2"}).text
    assert "2000–2099" in sub and "2 Projekte" in sub and "Rathaus" not in sub
    proj = c.get("/", params={"o": "2", "p": "20"}).text
    assert "Rathaus" in proj and "Schule" in proj and "Halle" not in proj and "2 Unterprojekte" in proj


def test_columns_view_and_filter_still_list(tmp_path, fake_ai):
    c, _ = _client(tmp_path, fake_ai)
    c.post("/einstellungen", data={"ansicht": "spalten"})
    assert 'class="cols"' in c.get("/").text and "Halle" not in c.get("/").text
    assert "Halle" in c.get("/", params={"o": "3", "p": "31"}).text
    assert "Schule" in c.get("/", params={"q": "Schule"}).text  # the filter works in every view


def test_unknown_view_is_refused(tmp_path, fake_ai):
    c, _ = _client(tmp_path, fake_ai)
    assert c.post("/einstellungen", data={"ansicht": "x"}).status_code == 400
    assert c.get("/einstellungen").status_code == 200
