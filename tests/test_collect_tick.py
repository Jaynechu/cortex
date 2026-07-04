from __future__ import annotations

import subprocess

from cortex import collect_tick


def _usage_cfg(base_cfg, **tick_overrides):
    cfg = dict(base_cfg)
    cfg["marrow"] = {"venv_python": "/fake/venv/python"}
    cfg["tick"] = {"usage_snapshot": True, **tick_overrides}
    return cfg


def _log_rows(conn):
    return conn.execute(
        "SELECT source, ok, error FROM ct_collector_log WHERE source='usage'"
    ).fetchall()


def test_usage_snapshot_success_logs_ok(monkeypatch, marrow_conn, base_cfg):
    cfg = _usage_cfg(base_cfg)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(collect_tick.subprocess, "run", fake_run)

    collect_tick._run_usage_snapshot(marrow_conn, cfg)

    assert captured["cmd"] == ["/fake/venv/python", "-m", "marrow.usage_snapshot"]
    rows = _log_rows(marrow_conn)
    assert len(rows) == 1
    assert rows[0]["ok"] == 1
    assert rows[0]["error"] is None


def test_usage_snapshot_failure_logs_error(monkeypatch, marrow_conn, base_cfg):
    cfg = _usage_cfg(base_cfg)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no oauth token available")

    monkeypatch.setattr(collect_tick.subprocess, "run", fake_run)

    collect_tick._run_usage_snapshot(marrow_conn, cfg)

    rows = _log_rows(marrow_conn)
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert "no oauth token available" in rows[0]["error"]


def test_usage_snapshot_timeout_logs_error_never_raises(monkeypatch, marrow_conn, base_cfg):
    cfg = _usage_cfg(base_cfg)

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(collect_tick.subprocess, "run", fake_run)

    collect_tick._run_usage_snapshot(marrow_conn, cfg)  # must not raise

    rows = _log_rows(marrow_conn)
    assert len(rows) == 1
    assert rows[0]["ok"] == 0


def test_usage_snapshot_config_gate_off_skips_entirely(monkeypatch, marrow_conn, base_cfg):
    cfg = _usage_cfg(base_cfg, usage_snapshot=False)

    def fake_run(cmd, **kw):
        raise AssertionError("subprocess.run must not be called when gated off")

    monkeypatch.setattr(collect_tick.subprocess, "run", fake_run)

    collect_tick._run_usage_snapshot(marrow_conn, cfg)

    assert _log_rows(marrow_conn) == []


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
