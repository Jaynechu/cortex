from __future__ import annotations

import sqlite3

import pytest

from cortex import db


@pytest.fixture
def marrow_conn(tmp_path):
    conn = db.connect_path(tmp_path / "marrow.db")
    yield conn
    conn.close()


@pytest.fixture
def base_cfg(tmp_path):
    return {
        "core": {"timezone": "Australia/Melbourne"},
        "paths": {
            "marrow_db": str(tmp_path / "marrow.db"),
            "knowledgec_db": "",
            "geofence_file": "",
            "health_export": "",
        },
        "knowledgec": {"stream_name": "/app/usage", "categories": {"default": "uncategorized"}},
        "geofence": {"enabled": False},
        "health": {"enabled": False},
    }


def make_knowledgec_fixture(path, rows):
    """rows: list of (bundle_id, start_coredata, end_coredata) seconds since
    2001-01-01 UTC, matching real ZOBJECT column semantics."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ZOBJECT (ZSTREAMNAME TEXT, ZVALUESTRING TEXT, ZSTARTDATE REAL, ZENDDATE REAL)"
    )
    conn.executemany(
        "INSERT INTO ZOBJECT (ZSTREAMNAME, ZVALUESTRING, ZSTARTDATE, ZENDDATE) VALUES ('/app/usage', ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
