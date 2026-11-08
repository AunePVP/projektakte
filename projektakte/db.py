"""SQLite storage: one database file under DATA_DIR, additive migrations via PRAGMA user_version.

Everything in the database is derived from the (read-only) sources and can be rebuilt – except
human decisions (mail assignments), which live in their own table.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[str] = [
    # 1 – project register
    """
    CREATE TABLE projects (
        number      TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        path        TEXT NOT NULL,
        active      INTEGER NOT NULL DEFAULT 1,
        first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """,
    # 2 – documents, sections (chunks) and full-text index
    """
    CREATE TABLE documents (
        id          INTEGER PRIMARY KEY,
        path        TEXT NOT NULL UNIQUE,
        project     TEXT,
        mail_id     INTEGER,
        rel_path    TEXT NOT NULL,
        filename    TEXT NOT NULL,
        ext         TEXT NOT NULL,
        size        INTEGER,
        mtime       REAL,
        sha256      TEXT,
        doc_date    TEXT,
        pages       INTEGER,
        status      TEXT NOT NULL DEFAULT 'ok',
        message     TEXT,
        method      TEXT,
        indexed_at  TEXT
    );
    CREATE INDEX documents_project ON documents(project);
    CREATE INDEX documents_mail ON documents(mail_id);
    CREATE TABLE chunks (
        id          INTEGER PRIMARY KEY,
        doc_id      INTEGER REFERENCES documents(id) ON DELETE CASCADE,
        mail_id     INTEGER,
        project     TEXT,
        ord         INTEGER NOT NULL,
        page        INTEGER,
        heading     TEXT,
        text        TEXT NOT NULL,
        embedding   BLOB
    );
    CREATE INDEX chunks_doc ON chunks(doc_id);
    CREATE INDEX chunks_mail ON chunks(mail_id);
    CREATE INDEX chunks_project ON chunks(project);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(
        title, body, tokenize = 'unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER chunks_after_delete AFTER DELETE ON chunks BEGIN
        DELETE FROM chunks_fts WHERE rowid = old.id;
    END;
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: Path | str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            conn.executescript("BEGIN;" + script + f"PRAGMA user_version = {i}; COMMIT;")
    finally:
        conn.close()


@contextmanager
def session(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Connection for one unit of work; commits on success, rolls back on error."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
