import sqlite3
import os
from pathlib import Path

DB_NAME = "dashboard.db"


def _db_path():
    return Path(__file__).resolve().parent.parent / DB_NAME


def get_db():
    db_path = _db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    db_path = _db_path()
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS server_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            pid INTEGER,
            preset TEXT,
            log_path TEXT,
            started_at TEXT,
            port INTEGER
        );

        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo_id TEXT,
            filename TEXT,
            dest_path TEXT,
            bytes_done INTEGER DEFAULT 0,
            bytes_total INTEGER DEFAULT 0,
            status TEXT DEFAULT 'queued',
            error TEXT,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_downloads_status ON downloads(status);
    """)
    conn.commit()
    conn.close()
