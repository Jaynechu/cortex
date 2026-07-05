"""SQLite connection + schema for cortex own tables (ct_ prefix) on the
shared marrow DB (~/.config/marrow/marrow.db). Journal mode is owned by
marrow (DELETE convention, see marrow/storage.py) — cortex must never set
journal_mode itself. All timestamps are timezone-aware UTC ISO-8601
strings, never naive datetime.now().
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cortex.config import marrow_db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS ct_app_usage (
    date TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    seconds REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, bundle_id)
);

CREATE TABLE IF NOT EXISTS ct_category_usage (
    date TEXT NOT NULL,
    category TEXT NOT NULL,
    seconds REAL NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (date, category)
);

CREATE TABLE IF NOT EXISTS ct_geofence (
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    event TEXT NOT NULL,
    raw_line TEXT NOT NULL,
    source_file TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (date, time, event)
);

CREATE TABLE IF NOT EXISTS ct_geofence_cursor (
    source_file TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ct_health (
    date TEXT NOT NULL,
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (date, source, key)
);

CREATE TABLE IF NOT EXISTS ct_activity (
    ts TEXT NOT NULL,
    sid TEXT NOT NULL,
    channel TEXT NOT NULL,
    PRIMARY KEY (ts, sid)
);

CREATE TABLE IF NOT EXISTS ct_collector_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT
);

CREATE TABLE IF NOT EXISTS ct_wake_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    wake INTEGER NOT NULL,
    dry_run INTEGER NOT NULL,
    reasons TEXT,
    gated_by TEXT,
    explanation TEXT
);

CREATE TABLE IF NOT EXISTS ct_pacemaker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(cfg: dict) -> sqlite3.Connection:
    path = marrow_db_path(cfg)
    return connect_path(path)


def connect_path(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def log_collector_run(conn: sqlite3.Connection, source: str, ok: bool, error: str | None = None) -> None:
    conn.execute(
        "INSERT INTO ct_collector_log (source, ts, ok, error) VALUES (?, ?, ?, ?)",
        (source, utcnow_iso(), 1 if ok else 0, error),
    )
    conn.commit()
