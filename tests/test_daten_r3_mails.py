"""Round 3: moved .eml files are recognised by content first, by Message-ID only together with the
same date and file size – a new scan of a device with constant id and subject never takes over the
place and decision of a deleted or moved one. Attachment cache files of removed mails are deleted;
the demo purge says what it removed on the status page."""
import os
import sqlite3
import time
from pathlib import Path

import pytest

from projektakte import db
from projektakte.assign import assign_mails, decide
from projektakte.background import run_warnings
from projektakte.mails.eml_source import sync_eml_folder
from projektakte.mails.store import CACHE_SWEEP_META, prune_cache_files, sweep_cache

from .test_mails_ingest import write_eml

PATTERN = r"BS-\d{4}-\d{4}"
SCAN_ID = "<scan@mfp.local>"
SUBJECT = "Gescanntes Dokument"


@pytest.fixture()
def maildb(tmp_path):
    path = tmp_path / "x.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    for number, name in (("BS-2024-0001", "Testhalle"), ("BS-2024-0002", "Schule")):
        conn.execute("INSERT INTO projects(number, name, path) VALUES(?, ?, ?)", (number, name, str(tmp_path / number)))
    conn.commit()
    yield conn, tmp_path / "mails", tmp_path / "cache"
    conn.close()


def sync(conn, mail_dir, cache):
    return sync_eml_folder(conn, mail_dir, cache_dir=cache)


def assign(conn):
    assign_mails(conn, PATTERN, min_share=0.5, min_lead=0.2, vector_key="k")


def state(conn, name):
    row = conn.execute("SELECT project, assign_status, assign_reason FROM mails WHERE source_key = ?",
                       ("eml:" + name,)).fetchone()
    return tuple(row) if row else None


def scan(mail_dir, name, body, date, **kw):
    return write_eml(mail_dir / name, SUBJECT, body, msgid=SCAN_ID, date=date,
                     sender="Scanner <scanner@ib.example>", **kw)


def decided_scan(conn, mail_dir, cache, name, body, date, project="BS-2024-0001"):
    scan(mail_dir, name, body, date)
    sync(conn, mail_dir, cache)
    assign(conn)
    mail_id = conn.execute("SELECT id FROM mails WHERE source_key = ?", ("eml:" + name,)).fetchone()[0]
    decide(conn, mail_id, "assign", project, user="anna")
    return mail_id


# -- move detection -----------------------------------------------------------------------------------
def test_new_scan_does_not_take_over_a_deleted_scan(maildb):
    """F1: the decided scan is deleted, a new scan with the same id and subject arrives in the same run."""
    conn, mail_dir, cache = maildb
    write_eml(mail_dir / "andere.eml", "Andere Mail", "Text")  # MAIL_DIR never empty
    old_id = decided_scan(conn, mail_dir, cache, "f1.eml", "Begehung Tiefgarage Lüftung",
                          "Mon, 03 Nov 2025 12:00:00 +0100")
    (mail_dir / "f1.eml").unlink()
    scan(mail_dir, "f2.eml", "Rechnung Heizöl Lieferung", "Tue, 04 Nov 2025 08:00:00 +0100")
    stats = sync(conn, mail_dir, cache)
    assert (stats["moved"], stats["new"], stats["removed"]) == (0, 1, 1)
    assign(conn)
    assert state(conn, "f2.eml")[:2] == (None, "unassigned")
    assert "anna" not in state(conn, "f2.eml")[2]
    assert conn.execute("SELECT id FROM mails WHERE source_key = 'eml:f2.eml'").fetchone()[0] != old_id


def test_moved_scan_keeps_its_place_although_a_new_scan_comes_first(maildb):
    """F2: the decided scan moves into a subfolder while a new scan arrives in the top folder (which
    is processed first): the moved scan keeps id and decision, the new one gets nothing."""
    conn, mail_dir, cache = maildb
    old_id = decided_scan(conn, mail_dir, cache, "g1.eml", "Prüfbericht Brandmeldeanlage",
                          "Mon, 03 Nov 2025 12:30:00 +0100", project="BS-2024-0002")
    (mail_dir / "erledigt").mkdir()
    (mail_dir / "g1.eml").rename(mail_dir / "erledigt" / "g1.eml")
    scan(mail_dir, "a_g2.eml", "Urlaubsantrag Juli", "Tue, 04 Nov 2025 08:30:00 +0100")
    stats = sync(conn, mail_dir, cache)
    assert (stats["moved"], stats["new"], stats["removed"]) == (1, 1, 0)
    assign(conn)
    assert conn.execute("SELECT id FROM mails WHERE source_key = 'eml:erledigt/g1.eml'").fetchone()[0] == old_id
    assert state(conn, "erledigt/g1.eml") == ("BS-2024-0002", "confirmed", "Von anna zugeordnet.")
    assert state(conn, "a_g2.eml")[:2] == (None, "unassigned")


def test_same_scan_with_hash_of_an_older_version_is_still_recognised(maildb):
    """An unchanged file whose stored content hash came from an older version (e.g. the body was
    decoded differently) is matched by Message-ID, date and file size."""
    conn, mail_dir, cache = maildb
    old_id = decided_scan(conn, mail_dir, cache, "s.eml", "Begehungsprotokoll", "Mon, 03 Nov 2025 12:00:00 +0100")
    conn.execute("UPDATE mails SET sha256 = 'aus-alter-version' WHERE id = ?", (old_id,))
    conn.commit()
    (mail_dir / "archiv").mkdir()
    (mail_dir / "s.eml").rename(mail_dir / "archiv" / "s.eml")
    assert sync(conn, mail_dir, cache)["moved"] == 1
    assign(conn)
    assert conn.execute("SELECT id FROM mails WHERE source_key = 'eml:archiv/s.eml'").fetchone()[0] == old_id
    assert state(conn, "archiv/s.eml")[1] == "confirmed"


def test_message_id_and_date_alone_do_not_move(maildb):
    """Same id, subject and date, but another file size and content: a different scan."""
    conn, mail_dir, cache = maildb
    write_eml(mail_dir / "andere.eml", "Andere Mail", "Text")
    decided_scan(conn, mail_dir, cache, "s1.eml", "Kurz", "Mon, 03 Nov 2025 12:00:00 +0100")
    (mail_dir / "s1.eml").unlink()
    scan(mail_dir, "s2.eml", "Ein ganz anderer, viel längerer Scan mit Lieferschein und Rechnung",
         "Mon, 03 Nov 2025 12:00:00 +0100")
    assert sync(conn, mail_dir, cache)["moved"] == 0


def test_two_candidates_for_one_vanished_scan_move_nothing(maildb):
    conn, mail_dir, cache = maildb
    write_eml(mail_dir / "andere.eml", "Andere Mail", "Text")
    mail_id = decided_scan(conn, mail_dir, cache, "s.eml", "Protokoll A", "Mon, 03 Nov 2025 12:00:00 +0100")
    conn.execute("UPDATE mails SET sha256 = 'alt' WHERE id = ?", (mail_id,))
    conn.commit()
    data = (mail_dir / "s.eml").read_bytes()
    (mail_dir / "s.eml").unlink()
    for name in ("x1.eml", "x2.eml"):  # two files fit (same id, date, size), neither has the same content
        (mail_dir / name).write_bytes(data.replace(b"Protokoll A", b"Protokoll " + name[1:2].encode()))
    stats = sync(conn, mail_dir, cache)
    assert (stats["moved"], stats["new"], stats["removed"]) == (0, 2, 1)


def test_moved_copies_with_identical_content_keep_one_mail_each(maildb):
    conn, mail_dir, cache = maildb
    write_eml(mail_dir / "a.eml", "Frage zur Halle", "Wie breit wird das Tor?", msgid="<q@x.example>")
    write_eml(mail_dir / "b.eml", "Andere Frage", "Wie hoch wird das Tor?", msgid="<r@x.example>")
    sync(conn, mail_dir, cache)
    ids = {r[0]: r[1] for r in conn.execute("SELECT source_key, id FROM mails")}
    (mail_dir / "neu").mkdir()
    (mail_dir / "a.eml").rename(mail_dir / "neu" / "a.eml")
    (mail_dir / "b.eml").rename(mail_dir / "neu" / "b.eml")
    stats = sync(conn, mail_dir, cache)
    assert (stats["moved"], stats["new"], stats["removed"]) == (2, 0, 0)
    now = {r[0]: r[1] for r in conn.execute("SELECT source_key, id FROM mails")}
    assert now == {"eml:neu/a.eml": ids["eml:a.eml"], "eml:neu/b.eml": ids["eml:b.eml"]}


# -- attachment cache ---------------------------------------------------------------------------------
def cache_files(cache: Path) -> list[Path]:
    return sorted(f for f in (cache / "attachments").rglob("*") if f.is_file()) if cache.exists() else []


def test_removed_mail_takes_its_attachment_files_along(maildb):
    conn, mail_dir, cache = maildb
    write_eml(mail_dir / "a.eml", "Mängel", "siehe Anhang", attachments=[("Liste.txt", b"Feuerloescher fehlt")])
    write_eml(mail_dir / "b.eml", "Mängel 2", "siehe Anhang", attachments=[("Liste.txt", b"Feuerloescher fehlt"),
                                                                           ("Nur_b.txt", b"nur in b")])
    write_eml(mail_dir / "c.eml", "Bleibt", "Text")
    sync(conn, mail_dir, cache)
    assert len(cache_files(cache)) == 2  # the shared attachment is cached once
    (mail_dir / "b.eml").unlink()
    assert sync(conn, mail_dir, cache)["removed"] == 1
    remaining = cache_files(cache)
    assert [f.name for f in remaining] == ["Liste.txt"]  # still used by a.eml
    assert not (cache / "attachments" / "x").exists()
    (mail_dir / "a.eml").unlink()
    sync(conn, mail_dir, cache)
    assert cache_files(cache) == []
    assert [p for p in (cache / "attachments").iterdir()] == []  # empty folders removed too


def test_prune_only_touches_unused_files_inside_the_cache(maildb, tmp_path):
    conn, _mail_dir, cache = maildb
    outside = tmp_path / "fremd.txt"
    outside.write_text("nicht löschen")
    assert prune_cache_files(conn, cache, [str(outside), "", None]) == 0
    assert outside.exists()


def test_sweep_removes_old_orphans_once_a_day(maildb):
    conn, mail_dir, cache = maildb
    write_eml(mail_dir / "a.eml", "Mängel", "siehe Anhang", attachments=[("Liste.txt", b"Feuerloescher fehlt")])
    sync(conn, mail_dir, cache)
    used, = cache_files(cache)
    old = cache / "attachments" / "ab" / ("ab" + "0" * 62) / "Waise.pdf"
    young = cache / "attachments" / "cd" / ("cd" + "0" * 62) / "Neu.pdf"
    for f in (old, young):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    past = time.time() - 2 * 3600
    os.utime(old, (past, past))
    os.utime(used, (past, past))
    assert sweep_cache(conn, cache) == 1
    assert not old.exists() and not old.parent.exists() and young.exists() and used.exists()
    assert conn.execute("SELECT value FROM meta WHERE key = ?", (CACHE_SWEEP_META,)).fetchone()
    os.utime(young, (past, past))
    assert sweep_cache(conn, cache) is None  # not due yet
    assert sweep_cache(conn, cache, now=time.time() + 25 * 3600) == 1
    assert cache_files(cache) == [used]


def test_reused_cache_file_counts_as_young(maildb):
    """A file an import is using again must not look like an old orphan to a sweep meanwhile."""
    conn, mail_dir, cache = maildb
    write_eml(mail_dir / "a.eml", "Mängel", "siehe Anhang", attachments=[("Liste.txt", b"Feuerloescher fehlt")])
    sync(conn, mail_dir, cache)
    f, = cache_files(cache)
    past = time.time() - 5 * 3600
    os.utime(f, (past, past))
    write_eml(mail_dir / "b.eml", "Mängel 2", "noch einmal", attachments=[("Liste.txt", b"Feuerloescher fehlt")])
    sync(conn, mail_dir, cache)
    assert time.time() - f.stat().st_mtime < 60


def test_demo_purge_is_reported_in_the_warnings():
    stats = {"demo_purge": {"projects": 8, "documents": 36, "mails": 78, "cache_files": 18},
             "projects": {"warning": "Projektordner fehlt"}}
    assert run_warnings(stats) == [
        "Demo-Daten aus dem Index entfernt (DEMO_MODE=0): 8 Projekt(e) mit 36 Dokument(en), 78 Mail(s), "
        "18 Anhang-Datei(en) im Cache", "Projektordner fehlt"]
    assert run_warnings({"demo_purge": {"projects": 0, "documents": 0, "mails": 0, "cache_files": 0}}) == []


def test_demo_purge_in_the_status_and_cache_emptied(tmp_path, fake_ai):
    from fastapi.testclient import TestClient

    from projektakte.demo import generate
    from projektakte.indexer import run_index
    from projektakte.main import create_app

    from .conftest import fake_settings
    from .test_background import wait_for_runs

    generate(tmp_path / "demo")
    run_index(fake_settings(fake_ai, tmp_path, DEMO_MODE=1))
    real = tmp_path / "real" / "BS-2026-0001 Rathaus"
    real.mkdir(parents=True)
    (real / "Notiz.txt").write_text("Rathaus: Brandmeldeanlage.", encoding="utf-8")
    s = fake_settings(fake_ai, tmp_path, DEMO_MODE=0, INDEX_INTERVAL=0, PROJECTS_DIR=tmp_path / "real",
                      MAIL_DIR=tmp_path / "keine-mails")
    with TestClient(create_app(s)) as client:
        st = wait_for_runs(client, 1)
    warnings = st["indexing"]["last_warnings"]
    assert any(w.startswith("Demo-Daten aus dem Index entfernt") and "Anhang-Datei(en) im Cache" in w
               for w in warnings), warnings
    assert cache_files(s.cache_dir) == []
    conn = sqlite3.connect(s.db_path)
    assert conn.execute("SELECT COUNT(*) FROM documents WHERE cache_path != ''").fetchone()[0] == 0
