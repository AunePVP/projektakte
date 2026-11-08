"""ops-11: consistent online backup of the database plus a JSON export of the human decisions."""
import json
import sqlite3
import subprocess
import sys

from projektakte import backup, db


def make_db(tmp_path):
    path = tmp_path / "data" / "projektakte.sqlite3"
    db.init_db(path)
    with db.session(path) as conn:
        conn.execute("INSERT INTO projects(number, name, path) VALUES('BS-2024-0137', 'Schule', '/p')")
        conn.execute("INSERT INTO assign_decisions(source_key, action, project, decided_by, decided_at) "
                     "VALUES('eml:a', 'assign', 'BS-2024-0137', 'anna', '2026-11-01T10:00:00')")
    return path


def test_backup_includes_uncheckpointed_wal_data(tmp_path):
    path = make_db(tmp_path)
    writer = db.connect(path)  # app still running: data only in the WAL, not yet in the main file
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("INSERT INTO assign_decisions(source_key, action, decided_by, decided_at) "
                   "VALUES('jmap:b', 'none', 'ben', '2026-11-02T09:00:00')")
    writer.commit()
    try:
        result = backup.run_backup(path, tmp_path / "sicherung")
    finally:
        writer.close()
    copy = sqlite3.connect(result["database"])
    assert copy.execute("SELECT COUNT(*) FROM assign_decisions").fetchone()[0] == 2
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # one self-contained file
    copy.close()
    data = json.loads(result["decisions"].read_text(encoding="utf-8"))
    assert data["count"] == 2 and data["format"] == "projektakte-zuordnungen/1"
    assert {d["source_key"] for d in data["decisions"]} == {"eml:a", "jmap:b"}
    assert not list((tmp_path / "sicherung").glob("*.tmp"))


def test_backup_cli(tmp_path, monkeypatch):
    make_db(tmp_path)
    out = subprocess.run([sys.executable, "-m", "projektakte.backup", str(tmp_path / "ziel")],
                         env={"DATA_DIR": str(tmp_path / "data"), "PATH": ""}, capture_output=True, text=True,
                         check=True).stdout
    assert "Datenbank gesichert" in out and "1 Entscheidungen" in out
    assert len(list((tmp_path / "ziel").glob("projektakte-*.sqlite3"))) == 1
    assert len(list((tmp_path / "ziel").glob("zuordnungen-*.json"))) == 1


def test_backup_without_database_fails_clearly(tmp_path, capsys):
    assert backup.main([str(tmp_path / "ziel"), "--db", str(tmp_path / "fehlt.sqlite3")]) == 1
    assert "Keine Datenbank gefunden" in capsys.readouterr().err
    assert not (tmp_path / "fehlt.sqlite3").exists()  # nothing created at the wrong place
