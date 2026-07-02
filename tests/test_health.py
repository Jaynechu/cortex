from __future__ import annotations

import json

from cortex.collectors import health


def test_collect_disabled_is_noop(marrow_conn, base_cfg):
    health.collect(marrow_conn, base_cfg)
    count = marrow_conn.execute("SELECT COUNT(*) c FROM ct_health").fetchone()["c"]
    assert count == 0


def test_collect_flattens_nested_json(tmp_path, marrow_conn, base_cfg):
    export_path = tmp_path / "health.json"
    payload = {
        "date": "2026-07-02",
        "sleep": {"hours": 7.5, "score": 82},
        "weight_kg": 68.2,
    }
    export_path.write_text(json.dumps(payload))

    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["health_export"] = str(export_path)
    cfg["health"] = {"enabled": True}

    health.collect(marrow_conn, cfg)
    rows = {r["key"]: r["value"] for r in marrow_conn.execute("SELECT key, value FROM ct_health").fetchall()}
    assert rows["date"] == "2026-07-02"
    assert rows["sleep.hours"] == "7.5"
    assert rows["sleep.score"] == "82"
    assert rows["weight_kg"] == "68.2"


def test_collect_falls_back_to_mtime_date_when_no_date_field(tmp_path, marrow_conn, base_cfg):
    export_path = tmp_path / "health.json"
    export_path.write_text(json.dumps({"weight_kg": 68.0}))

    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["health_export"] = str(export_path)
    cfg["health"] = {"enabled": True}

    health.collect(marrow_conn, cfg)
    row = marrow_conn.execute("SELECT date FROM ct_health WHERE key='weight_kg'").fetchone()
    assert row["date"]  # some date got stamped, not blank


def test_collect_raises_when_enabled_but_file_missing(marrow_conn, base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["health_export"] = str(tmp_path / "missing.json")
    cfg["health"] = {"enabled": True}
    try:
        health.collect(marrow_conn, cfg)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
