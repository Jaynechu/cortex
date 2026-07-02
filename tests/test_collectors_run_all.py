from __future__ import annotations

from cortex.collectors import run_all


def test_run_all_isolates_failures(marrow_conn, base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["knowledgec_db"] = str(tmp_path / "missing.db")  # forces failure
    cfg["knowledgec"] = {"stream_name": "/app/usage", "categories": {"default": "uncategorized"}}
    # geofence + health left disabled -> succeed trivially

    results = run_all(marrow_conn, cfg)

    assert results["knowledgec"] is False
    assert results["geofence"] is True
    assert results["health"] is True

    log_rows = {
        r["source"]: r["ok"]
        for r in marrow_conn.execute("SELECT source, ok FROM ct_collector_log").fetchall()
    }
    assert log_rows["knowledgec"] == 0
    assert log_rows["geofence"] == 1
    assert log_rows["health"] == 1

    error = marrow_conn.execute(
        "SELECT error FROM ct_collector_log WHERE source='knowledgec'"
    ).fetchone()["error"]
    assert "missing.db" in error or "FileNotFoundError" in error
