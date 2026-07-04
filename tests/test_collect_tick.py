from __future__ import annotations

from cortex import collect_tick


def test_render_day_log_skips_quietly_when_file_missing(marrow_conn, base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    cfg["paths"]["day_log"] = str(tmp_path / "does_not_exist" / "day_log.md")

    collect_tick._render_day_log(marrow_conn, cfg)  # must not raise

    assert not (tmp_path / "does_not_exist").exists()


def test_render_day_log_updates_existing_file(marrow_conn, base_cfg, tmp_path):
    from cortex import day_log

    marrow_conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, session_id TEXT, "
        "timestamp TEXT, role TEXT, content TEXT, ts_start TEXT, ts_end TEXT)"
    )
    cfg = dict(base_cfg)
    cfg["paths"] = dict(base_cfg["paths"])
    path = tmp_path / "day_log.md"
    cfg["paths"]["day_log"] = str(path)
    day_log.new_day(path, "2026-07-04")
    before = path.read_text()
    assert "pending first update" in before

    marrow_conn.execute(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        ("2026-07-04T01:00:00+00:00", "sid1", "wx"),
    )
    marrow_conn.commit()

    collect_tick._render_day_log(marrow_conn, cfg)

    after = path.read_text()
    assert "pending first update" not in after
