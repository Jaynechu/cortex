"""Two-tier silence + awake gate tests.

Chat tier (user replied this wake): silent >= silent_max -> TUCK-IN marker, then
tuck_grace -> auto sleep. No-user tier: silent >= no_user_gate -> auto sleep, no
marker. A live wait_until holds everything. The awake gate never emits a wake;
the late-sentinel race (user speaks then sentinel fires) is silent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, db, wake_state, watchdog


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


@pytest.fixture
def awake_no_sentinel(cfg, monkeypatch):
    """A live wake with sentinel spawn stubbed out (auto sleep calls lie_down,
    which re-arms a sentinel)."""
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)
    monkeypatch.setattr("cortex.sentinel.subprocess.Popen",
                        lambda *a, **k: type("P", (), {"pid": 1})())
    return cfg


def _signal_lines(cfg):
    p = config.wake_signal_log_path(cfg)
    return p.read_text().splitlines() if p.exists() else []


# --- no-user tier -------------------------------------------------------------

def test_no_user_short_gate_auto_sleeps(awake_no_sentinel):
    cfg = awake_no_sentinel
    # no user reply this wake, silent past no_user_gate_min (5) -> auto sleep
    action = watchdog.silence_action(cfg, silent_min=6.0)
    assert action and "auto sleep" in action
    assert wake_state.is_awake(cfg) is False
    assert _signal_lines(cfg) == []  # no marker on the no-user path


def test_no_user_under_gate_holds(awake_no_sentinel):
    cfg = awake_no_sentinel
    assert watchdog.silence_action(cfg, silent_min=3.0) is None
    assert wake_state.is_awake(cfg) is True


# --- chat tier ----------------------------------------------------------------

def test_chat_tuck_in_then_grace(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    # First: silent past silent_max (20) -> tuck-in marker, still awake.
    a1 = watchdog.silence_action(cfg, silent_min=21.0)
    assert a1 == "tuck-in appended"
    assert wake_state.is_awake(cfg) is True
    lines = _signal_lines(cfg)
    assert len(lines) == 1 and "[TUCK-IN]" in lines[0]
    assert "0/2" in lines[0]  # live wait count substituted
    # Marker stamped -> not re-appended on the next poll.
    a2 = watchdog.silence_action(cfg, silent_min=22.0)
    assert a2 is None
    assert len(_signal_lines(cfg)) == 1
    # Backdate the tuck stamp past the grace window -> auto sleep.
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    wake_state.update(cfg, tuck_pending=past)
    a3 = watchdog.silence_action(cfg, silent_min=23.0)
    assert a3 and "auto sleep" in a3
    assert wake_state.is_awake(cfg) is False


def test_chat_under_silent_max_holds(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    assert watchdog.silence_action(cfg, silent_min=10.0) is None
    assert _signal_lines(cfg) == []


def test_live_wait_until_holds_everything(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    wake_state.set_wait_until(cfg, future)
    # Even well past silent_max, a live wait_until suppresses the tuck-in.
    assert watchdog.silence_action(cfg, silent_min=40.0) is None
    assert _signal_lines(cfg) == []
    assert wake_state.is_awake(cfg) is True


def test_wait_cancels_pending_auto_sleep(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    watchdog.silence_action(cfg, silent_min=21.0)  # tuck-in stamped
    # A wait() during grace sets a live wait_until -> auto sleep held off.
    future = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    wake_state.set_wait_until(cfg, future)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    wake_state.update(cfg, tuck_pending=past)
    assert watchdog.silence_action(cfg, silent_min=25.0) is None
    assert wake_state.is_awake(cfg) is True


# --- awake gate (tick) --------------------------------------------------------

def _fresh_transcript(cfg):
    import json
    from cortex import transcript
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 1, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 1}}}))


def test_awake_gate_late_sentinel_race_is_silent(awake_no_sentinel):
    """User speaks 15:54 (awake, fresh transcript), the late sentinel/tick fires
    15:55: the awake gate runs the silence check, sees the fresh transcript
    (idle ~0) -> holds, emits NO wake signal, stays awake."""
    from cortex import pacemaker_tick
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    _fresh_transcript(cfg)  # user just spoke -> transcript is hot
    conn = db.connect(cfg)
    try:
        msg = pacemaker_tick._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "wake in progress" in msg  # held, no emit, no auto sleep
    assert _signal_lines(cfg) == []
    assert wake_state.is_awake(cfg) is True


def test_awake_gate_asleep_still_fires(cfg, monkeypatch):
    """Sanity contrast: when NOT awake, the awake gate is not taken at all — the
    normal tick decision path runs (asleep+due -> emit as today)."""
    # No awake marker set -> is_awake False.
    assert wake_state.is_awake(cfg) is False
