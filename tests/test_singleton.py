"""Phase 4 — resident singleton audit.

Invariant: exactly ONE live cortex resident at any time (0 and 2 are both
wrong), unless night-sleep or explicit pause/mute. These tests simulate the
three incident shapes with state-file fixtures and assert self-heal:
  - double-wake (2 residents): watchdog.spawn is idempotent (a live recorded
    pid = no second spawn); ctl.cmd_wake on an alive+awake window is a no-op.
  - watchdog-death-during-wait: the tick awake gate respawns a dead watchdog.
  - stale epoch: a superseded snapshot (gen moved) holds, no respawn/reap.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cortex import config, db, pacemaker_tick, wake_state, watchdog


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    c["paths"]["ny_db_pages"] = str(tmp_path / "ny")
    c["wake"]["sentinel"] = False
    return c


# --- _pid_alive ---------------------------------------------------------------

def test_pid_alive_self_true():
    import os
    assert watchdog._pid_alive(os.getpid()) is True


def test_pid_alive_none_and_dead_false():
    assert watchdog._pid_alive(None) is False
    assert watchdog._pid_alive(0) is False
    # a very high pid is almost certainly not a live process
    assert watchdog._pid_alive(4_000_000_000) is False


# --- spawn singleton guard (double-watchdog prevention) -----------------------

def test_spawn_skips_when_recorded_pid_alive(cfg, monkeypatch):
    """A live recorded watchdog pid = already on duty: spawn returns it, never
    launches a second subprocess."""
    import os
    wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 999})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 0  # no second watchdog
    assert pid == os.getpid()  # returns the live one


def test_spawn_launches_when_no_record(cfg, monkeypatch):
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4242})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1
    assert pid == 4242


def test_spawn_launches_when_recorded_pid_dead(cfg, monkeypatch):
    wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead pid
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4243})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1  # dead record -> fresh spawn
    assert pid == 4243


# --- ctl.cmd_wake already-on-duty guard (double-wake prevention) --------------

def test_ctl_wake_alive_awake_is_noop(cfg, monkeypatch):
    """A resident window that is alive AND awake is already on duty: cmd_wake
    must NOT drive run_wake (which would re-set_awake + spawn a 2nd watchdog)."""
    from cortex import ctl, wake
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    wake_state.set_awake(cfg, 1, None)

    def _boom(*a, **k):
        raise AssertionError("run_wake must not run for an already-awake resident")
    monkeypatch.setattr("cortex.wake.run_wake", _boom)
    msg = ctl.cmd_wake(cfg)
    assert "already awake" in msg
    assert wake_state.is_awake(cfg) is True  # still exactly one resident


def test_ctl_wake_alive_dormant_still_wakes(cfg, monkeypatch):
    """Alive-but-dormant (not awake) still takes the standard ear wake path."""
    from cortex import ctl, wake
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    ran = {"n": 0}
    monkeypatch.setattr("cortex.wake.run_wake",
                        lambda conn, c, decision, now=None:
                        ran.__setitem__("n", ran["n"] + 1) or {"mode": "window"})
    ctl.cmd_wake(cfg)
    assert ran["n"] == 1  # dormant resident IS woken


# --- awake-gate watchdog-liveness heal (watchdog death during a wait) ---------

def _awake_window(cfg, conn):
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    wake_state.set_awake(cfg, wid, None)
    return wid


def test_awake_gate_respawns_dead_watchdog(cfg, monkeypatch):
    """Watchdog died mid-wake: the 5-min tick awake gate must respawn one so the
    resident regains 60s polling + exact-time fuse."""
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        snap_gen = st.get("gen")
        wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead
        monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
        monkeypatch.setattr(watchdog, "silence_action", lambda *a, **k: None)
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        pacemaker_tick._handle_awake(conn, cfg, st, snap_gen=snap_gen)
        assert respawned["n"] == 1  # dead watchdog respawned
    finally:
        conn.close()


def test_awake_gate_no_respawn_when_watchdog_alive(cfg, monkeypatch):
    import os
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        snap_gen = st.get("gen")
        wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))  # alive
        monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
        monkeypatch.setattr(watchdog, "silence_action", lambda *a, **k: None)
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        pacemaker_tick._handle_awake(conn, cfg, st, snap_gen=snap_gen)
        assert respawned["n"] == 0  # live watchdog -> no second spawn
    finally:
        conn.close()


def test_awake_gate_stale_epoch_holds_no_respawn(cfg, monkeypatch):
    """Stale snapshot (gen moved since the tick opened = a user reset / lie_down):
    the awake gate must hold and NOT respawn a watchdog against a dead epoch."""
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        stale_gen = (st.get("gen") or 0) - 1  # snapshot older than live
        wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        msg = pacemaker_tick._handle_awake(conn, cfg, st, snap_gen=stale_gen)
        assert "superseded" in msg
        assert respawned["n"] == 0  # never respawn against a stale epoch
    finally:
        conn.close()
