from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from cortex.collectors import knowledgec
from tests.conftest import make_knowledgec_fixture

TZ = ZoneInfo("Australia/Melbourne")
COREDATA_EPOCH_OFFSET = knowledgec.COREDATA_EPOCH_OFFSET


def _coredata(dt_local: datetime) -> float:
    return dt_local.timestamp() - COREDATA_EPOCH_OFFSET


def test_collect_aggregates_per_app_and_category(tmp_path, marrow_conn, base_cfg):
    kc_path = tmp_path / "knowledgeC.db"
    start = datetime(2026, 1, 15, 10, 0, 0, tzinfo=TZ)
    end = datetime(2026, 1, 15, 10, 5, 0, tzinfo=TZ)  # 300s
    start2 = datetime(2026, 1, 15, 11, 0, 0, tzinfo=TZ)
    end2 = datetime(2026, 1, 15, 11, 2, 0, tzinfo=TZ)  # 120s, other app same category

    rows = [
        ("com.googlecode.iterm2", _coredata(start), _coredata(end)),
        ("com.example.editor", _coredata(start2), _coredata(end2)),
    ]
    make_knowledgec_fixture(kc_path, rows)

    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["knowledgec_db"] = str(kc_path)
    cfg["knowledgec"] = {
        "stream_name": "/app/usage",
        "categories": {"com.googlecode.iterm2": "dev", "com.example.editor": "dev", "default": "uncategorized"},
    }

    knowledgec.collect(marrow_conn, cfg)

    app_rows = {
        (r["date"], r["bundle_id"]): r["seconds"]
        for r in marrow_conn.execute("SELECT date, bundle_id, seconds FROM ct_app_usage").fetchall()
    }
    assert app_rows[("2026-01-15", "com.googlecode.iterm2")] == 300.0
    assert app_rows[("2026-01-15", "com.example.editor")] == 120.0

    cat_rows = {
        (r["date"], r["category"]): r["seconds"]
        for r in marrow_conn.execute("SELECT date, category, seconds FROM ct_category_usage").fetchall()
    }
    assert cat_rows[("2026-01-15", "dev")] == 420.0


def test_collect_is_idempotent_on_rerun(tmp_path, marrow_conn, base_cfg):
    kc_path = tmp_path / "knowledgeC.db"
    start = datetime(2026, 1, 15, 10, 0, 0, tzinfo=TZ)
    end = datetime(2026, 1, 15, 10, 5, 0, tzinfo=TZ)
    make_knowledgec_fixture(kc_path, [("com.example.app", _coredata(start), _coredata(end))])

    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["knowledgec_db"] = str(kc_path)
    cfg["knowledgec"] = {"stream_name": "/app/usage", "categories": {"default": "uncategorized"}}

    knowledgec.collect(marrow_conn, cfg)
    knowledgec.collect(marrow_conn, cfg)

    count = marrow_conn.execute("SELECT COUNT(*) c FROM ct_app_usage").fetchone()["c"]
    assert count == 1


def test_collect_raises_when_db_missing(marrow_conn, base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["knowledgec_db"] = str(tmp_path / "missing.db")
    cfg["knowledgec"] = {"stream_name": "/app/usage", "categories": {"default": "uncategorized"}}

    try:
        knowledgec.collect(marrow_conn, cfg)
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
