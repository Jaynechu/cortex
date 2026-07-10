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


def test_render_daybrief_shells_out_to_marrow_venv(monkeypatch, marrow_conn, base_cfg):
    cfg = _usage_cfg(base_cfg)
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(collect_tick.subprocess, "run", fake_run)

    collect_tick._render_daybrief(marrow_conn, cfg)

    assert captured["cmd"] == ["/fake/venv/python", "-m", "marrow.daybrief"]
    rows = marrow_conn.execute(
        "SELECT ok, error FROM ct_collector_log WHERE source='daybrief'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ok"] == 1
    assert rows[0]["error"] is None


def test_render_daybrief_failure_logs_error_never_raises(monkeypatch, marrow_conn, base_cfg):
    cfg = _usage_cfg(base_cfg)

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(collect_tick.subprocess, "run", fake_run)

    collect_tick._render_daybrief(marrow_conn, cfg)  # must not raise

    rows = marrow_conn.execute(
        "SELECT ok FROM ct_collector_log WHERE source='daybrief'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
