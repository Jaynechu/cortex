"""Sentinel (exact-time wake) tests: arm on lie_down with the right sleep secs,
re-arm kills the predecessor, pid lifecycle + self-guarded clear, sentinel=false
skips the spawn. subprocess.Popen and os.kill are stubbed — no real process."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, db, lie_down, sentinel, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


_ORIG_SPAWN = sentinel.spawn  # captured before conftest's autouse no-op patch


@pytest.fixture(autouse=True)
def _real_spawn(monkeypatch):
    """This module exercises the REAL sentinel.spawn (with a stubbed Popen), so
    undo conftest's autouse spawn no-op for this module."""
    monkeypatch.setattr(sentinel, "spawn", _ORIG_SPAWN)


def _seed_wake(cfg):
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)


def test_spawn_computes_sleep_seconds(cfg, monkeypatch):
    captured = {}

    def fake_popen(cmd, *a, **kw):
        captured["cmd"] = cmd
        class P: pid = 4242
        return P()

    monkeypatch.setattr("cortex.sentinel.subprocess.Popen", fake_popen)
    pid = sentinel.spawn(cfg, 123.0)
    assert pid == 4242
    assert "--seconds" in captured["cmd"]
    assert "123.0" in captured["cmd"]


def test_lie_down_arms_sentinel_with_pid(cfg, monkeypatch):
    _seed_wake(cfg)
    monkeypatch.setattr("cortex.sentinel.subprocess.Popen",
                        lambda *a, **k: type("P", (), {"pid": 999})())
    r = lie_down.lie_down(cfg, next_wake_min=20)
    assert r["next_wake"] is not None
    assert wake_state.get_sentinel_pid(cfg) == 999


def test_lie_down_kills_predecessor_then_rearms(cfg, monkeypatch):
    _seed_wake(cfg)
    wake_state.set_sentinel_pid(cfg, 111)  # a stale predecessor
    killed = []
    monkeypatch.setattr("cortex.lie_down.os.kill",
                        lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("cortex.sentinel.subprocess.Popen",
                        lambda *a, **k: type("P", (), {"pid": 222})())
    lie_down.lie_down(cfg, next_wake_min=15)
    assert 111 in killed  # predecessor SIGTERM'd
    assert wake_state.get_sentinel_pid(cfg) == 222  # fresh armed


def test_sentinel_false_skips_spawn(cfg, monkeypatch):
    _seed_wake(cfg)
    cfg["wake"]["sentinel"] = False
    spawned = []
    monkeypatch.setattr("cortex.sentinel.subprocess.Popen",
                        lambda *a, **k: spawned.append(1) or type("P", (), {"pid": 1})())
    lie_down.lie_down(cfg, next_wake_min=20)
    assert spawned == []  # tick-only mode: no sentinel
    assert wake_state.get_sentinel_pid(cfg) is None


def test_sentinel_run_clears_own_pid_self_guarded(cfg, monkeypatch):
    import os
    wake_state.set_sentinel_pid(cfg, os.getpid())
    monkeypatch.setattr("cortex.pacemaker_tick.main", lambda: 0)
    sentinel.run(cfg, 0.0)
    assert wake_state.get_sentinel_pid(cfg) is None  # cleared (matched own pid)


def test_sentinel_run_leaves_newer_pid(cfg, monkeypatch):
    # A newer lie_down re-armed a different pid before this sentinel fired: it
    # must NOT clobber that record.
    wake_state.set_sentinel_pid(cfg, 777_777)  # not our pid
    monkeypatch.setattr("cortex.pacemaker_tick.main", lambda: 0)
    sentinel.run(cfg, 0.0)
    assert wake_state.get_sentinel_pid(cfg) == 777_777
