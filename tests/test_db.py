from __future__ import annotations

from cortex import db


def test_migrate_creates_all_tables(marrow_conn):
    tables = {
        row["name"]
        for row in marrow_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ct_%'"
        ).fetchall()
    }
    expected = {
        "ct_app_usage",
        "ct_category_usage",
        "ct_geofence",
        "ct_geofence_cursor",
        "ct_health",
        "ct_activity",
        "ct_collector_log",
    }
    assert expected.issubset(tables)


def test_migrate_is_idempotent(marrow_conn):
    db.migrate(marrow_conn)
    db.migrate(marrow_conn)  # must not raise


def test_log_collector_run(marrow_conn):
    db.log_collector_run(marrow_conn, "knowledgec", ok=True)
    db.log_collector_run(marrow_conn, "geofence", ok=False, error="boom")
    rows = marrow_conn.execute("SELECT source, ok, error FROM ct_collector_log ORDER BY id").fetchall()
    assert rows[0]["source"] == "knowledgec"
    assert rows[0]["ok"] == 1
    assert rows[0]["error"] is None
    assert rows[1]["source"] == "geofence"
    assert rows[1]["ok"] == 0
    assert rows[1]["error"] == "boom"
