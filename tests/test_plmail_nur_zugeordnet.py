"""plMail: only mails a human assigned to a project are downloaded and stored (default); the content of
other mails is never fetched, a taken-back assignment removes the copy, and mails_bereinigen removes the
unassigned copies an older version made."""
import json
import sqlite3

import pytest

from projektakte import db, mails_bereinigen
from projektakte.demo.writers import pdf_bytes
from projektakte.mails.jmap_source import JMAPClient, sync_plmail

from .fake_jmap import TOKEN, start_fake_plmail

CONTENT = {"subject", "bodyValues", "textBody", "htmlBody", "attachments"}


@pytest.fixture()
def plmail(tmp_path):
    server, fake = start_fake_plmail()
    db.init_db(tmp_path / "x.sqlite3")
    yield f"http://127.0.0.1:{server.server_address[1]}", fake, tmp_path
    server.shutdown()
    server.server_close()


def sync(url, tmp_path, **kw):
    with db.session(tmp_path / "x.sqlite3") as conn:
        return sync_plmail(conn, JMAPClient(url, TOKEN, page_size=2), cache_dir=tmp_path / "cache", **kw)


def stored(tmp_path) -> dict[str, dict]:
    conn = sqlite3.connect(tmp_path / "x.sqlite3")
    conn.row_factory = sqlite3.Row
    return {r["source_key"].rsplit(":", 1)[1]: dict(r) for r in conn.execute("SELECT * FROM mails")}


def attachments(tmp_path) -> int:
    with sqlite3.connect(tmp_path / "x.sqlite3") as conn:
        return conn.execute("SELECT COUNT(*) FROM documents WHERE mail_id IS NOT NULL").fetchone()[0]


def fetched_with_content(fake) -> set[str]:
    return {i for ids, props in fake.gets if props is None or CONTENT & set(props) for i in ids}


def test_only_assigned_mails_are_fetched_and_stored(plmail):
    url, fake, tmp = plmail
    pdf = [("Bericht.pdf", pdf_bytes(["Privat"]), "application/pdf")]
    private = fake.add("7", "Urlaub", "Privates", attachments=pdf)
    project = fake.add("7", "BS-2024-0137 Treppe", "Bitte prüfen.", attachments=pdf)
    nothing = fake.add("7", "Newsletter", "Werbung")
    fake.assign("7", project, "BS-2024-0137")
    fake.assign("7", nothing, None, status="none")
    sync(url, tmp)
    assert set(stored(tmp)) == {project}
    assert fetched_with_content(fake) == {project}  # the content of the others never left plMail
    assert fake.downloads == fake.blob_ids("7", project)
    assert attachments(tmp) == 1


def test_later_assignment_fetches_and_taking_it_back_removes(plmail):
    url, fake, tmp = plmail
    eid = fake.add("7", "Rückfrage", "Text", attachments=[("a.pdf", pdf_bytes(["x"]), "application/pdf")])
    sync(url, tmp)
    assert stored(tmp) == {}
    fake.assign("7", eid, "BS-2024-0137")
    sync(url, tmp)  # via Email/changes
    assert set(stored(tmp)) == {eid} and attachments(tmp) == 1
    fake.assign("7", eid, None, status="none")  # "kein Projektbezug"
    stats = sync(url, tmp)
    assert stored(tmp) == {} and attachments(tmp) == 0
    assert stats["accounts"]["7"]["removed"] == 1


def test_decision_made_in_projektakte_counts(plmail):
    url, fake, tmp = plmail
    fake.project_extension = False  # an older plMail without the project property
    eid = fake.add("7", "Alt", "Text")
    other = fake.add("7", "Privat", "Text")
    with db.session(tmp / "x.sqlite3") as conn:
        conn.execute("INSERT INTO assign_decisions(source_key, action, project) VALUES(?, 'assign', 'BS-1')",
                     (f"jmap:7:{eid}",))
        conn.commit()
    sync(url, tmp)
    assert set(stored(tmp)) == {eid} and other not in fetched_with_content(fake)


def test_all_mails_on_request(plmail):
    url, fake, tmp = plmail
    a, b = fake.add("7", "A", "x"), fake.add("7", "B", "y")
    sync(url, tmp, only_assigned=False)
    assert set(stored(tmp)) == {a, b}


def test_bereinigen_removes_the_unassigned_copies(plmail, capsys, monkeypatch):
    url, fake, tmp = plmail
    keep, drop = fake.add("7", "Projekt", "x"), fake.add("7", "Privat", "y",
                                                       attachments=[("p.pdf", pdf_bytes(["p"]), "application/pdf")])
    fake.assign("7", keep, "BS-2024-0137")
    sync(url, tmp, only_assigned=False)  # what an older version stored
    assert set(stored(tmp)) == {keep, drop}
    monkeypatch.setenv("DATA_DIR", str(tmp))
    monkeypatch.setattr(mails_bereinigen, "load_settings",
                        lambda: type("S", (), {"db_path": tmp / "x.sqlite3", "cache_dir": tmp / "cache"})())
    assert mails_bereinigen.main([]) == 0
    assert "ohne Projektzuordnung: 1" in capsys.readouterr().out and len(stored(tmp)) == 2  # only counted
    assert mails_bereinigen.main(["--loeschen"]) == 0
    assert set(stored(tmp)) == {keep} and attachments(tmp) == 0
    assert json.loads(stored(tmp)[keep]["external_assignment"])["project"] == "BS-2024-0137"
