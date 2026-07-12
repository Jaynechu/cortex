"""Ledger + tick reconcile + pause gating (schedule reliability fix).

Covers: next_wake_at write/clear, night clamp, the reconcile decision matrix
(alive-never-touch / rotated-vs-resume / accidental-close / future-hold), pause
gating, per-session _window_alive, and that the tick has no dangling catchup
import. No iTerm/claude here — all machine-touching calls are stubbed."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cortex import config, db, lie_down, pacemaker_tick, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    c["wake"]["sentinel"] = False  # no detached sentinel in tests
    return c


def _tz(cfg):
    return ZoneInfo(cfg["core"]["timezone"])


# --- ledger write/clear -------------------------------------------------------

def test_ledger_write_and_clear(cfg):
    assert wake_state.get_next_wake_at(cfg) is None
    wake_state.set_next_wake_at(cfg, "2026-07-13T09:00:00+10:00")
    assert wake_state.get_next_wake_at(cfg) == "2026-07-13T09:00:00+10:00"
    wake_state.clear_next_wake_at(cfg)
    assert wake_state.get_next_wake_at(cfg) is None


def test_lie_down_persists_ledger(cfg):
    wake_state.set_awake(cfg, 1, None)  # a wake in progress
    lie_down.lie_down(cfg, force_slept="auto", next_wake_min=30)
    assert wake_state.get_next_wake_at(cfg) is not None  # ledger written by _arm_sentinel


def test_set_awake_clears_ledger(cfg):
    wake_state.set_next_wake_at(cfg, "2026-07-13T09:00:00+10:00")
    wake_state.set_awake(cfg, 1, None)  # a fresh wake fired -> ledger consumed
    assert wake_state.get_next_wake_at(cfg) is None


# --- night clamp --------------------------------------------------------------

def test_night_clamp_pushes_to_gate_end(cfg):
    tz = _tz(cfg)
    cfg["gates"]["night"] = {"start": "23:00", "end": "08:00", "cap": 0}
    mid_night = datetime(2026, 7, 13, 2, 30, tzinfo=tz)  # inside the gate
    clamped = lie_down._clamp_to_night_end(cfg, mid_night)
    assert clamped.hour == 8 and clamped.minute == 0
    assert clamped.date() == mid_night.date()  # same morning


def test_night_clamp_leaves_daytime_untouched(cfg):
    tz = _tz(cfg)
    cfg["gates"]["night"] = {"start": "23:00", "end": "08:00", "cap": 0}
    noon = datetime(2026, 7, 13, 12, 0, tzinfo=tz)
    assert lie_down._clamp_to_night_end(cfg, noon) == noon


def test_arm_sentinel_returns_effective_clamped_time(cfg):
    """P2-3: _arm_sentinel must return the EFFECTIVE (post-night-clamp) time —
    the ledger, sentinel and lie_down()'s reported next_wake must all agree
    (never report the pre-clamp time, e.g. 02:00, when 08:00 was armed)."""
    tz = _tz(cfg)
    cfg["gates"]["night"] = {"start": "23:00", "end": "08:00", "cap": 0}
    cfg["wake"]["sentinel"] = False  # no detached process in tests
    mid_night = datetime(2026, 7, 13, 2, 0, tzinfo=tz)  # inside the gate
    effective = lie_down._arm_sentinel(cfg, mid_night)
    assert effective.hour == 8 and effective.minute == 0
    ledger = wake_state.get_next_wake_at(cfg)
    assert ledger is not None
    assert "08:00" in ledger  # ledger agrees with the returned effective time


def test_lie_down_reports_clamped_next_wake(cfg):
    """lie_down()'s reported next_wake must be the post-clamp HH:MM, matching
    what _arm_sentinel actually wrote to the ledger — regardless of whether the
    pre-clamp floor lands inside or outside the night gate."""
    cfg["gates"]["night"] = {"start": "23:00", "end": "08:00", "cap": 0}
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=20)
    ledger = wake_state.get_next_wake_at(cfg)
    assert ledger is not None and r["next_wake"] is not None
    assert r["next_wake"] in ledger  # HH:MM substring of the ISO ledger


# --- reconcile decision matrix ------------------------------------------------

def _fire_spy(monkeypatch):
    calls = {}

    def fake_fire(conn, cfg, why):
        calls["why"] = why
        return f"fired: {why}"

    monkeypatch.setattr(pacemaker_tick, "_fire_dead_window", fake_fire)
    return calls


def test_reconcile_alive_never_touched(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
    calls = _fire_spy(monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=5)).isoformat())  # overdue
    st = {"awake": True}
    assert pacemaker_tick._reconcile(None, cfg, st, now) is None
    assert "why" not in calls  # alive window is never fired at


def test_reconcile_due_ledger_dead_window_fires(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=1)).isoformat())
    msg = pacemaker_tick._reconcile(None, cfg, {}, now)
    assert "ledger due" in calls["why"]
    assert msg.startswith("fired:")


def test_reconcile_future_ledger_holds(cfg, monkeypatch):
    """A future ledger alarm is authoritative: _reconcile must return a hold
    (not None) so main() short-circuits and no other wake path (e.g. an
    overdue floor) can fire early, e.g. right after `ctl sleep --min 30`."""
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now + timedelta(minutes=20)).isoformat())
    msg = pacemaker_tick._reconcile(None, cfg, {}, now)
    assert msg is not None and "hold" in msg.lower()
    assert "why" not in calls  # future alarm -> caught at due time, no re-arm


def test_reconcile_accidental_close_resumes(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    wake_state.set_session_id(cfg, "SID-1")
    wake_state.update(cfg, awake=True)  # awake, no next_wake_at
    now = datetime.now(_tz(cfg))
    st = wake_state.load(cfg)
    msg = pacemaker_tick._reconcile(None, cfg, st, now)
    assert "accidental close" in calls["why"]
    assert msg.startswith("fired:")


def test_reconcile_paused_holds_everything(cfg, monkeypatch):
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    calls = _fire_spy(monkeypatch)
    wake_state.set_paused(cfg, True)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now - timedelta(minutes=5)).isoformat())  # overdue
    msg = pacemaker_tick._reconcile(None, cfg, {}, now)
    assert "paused" in msg.lower()
    assert "why" not in calls  # nothing fires while paused


def test_pause_flag_roundtrip(cfg):
    assert wake_state.is_paused(cfg) is False
    wake_state.set_paused(cfg, True)
    assert wake_state.is_paused(cfg) is True
    wake_state.set_paused(cfg, False)
    assert wake_state.is_paused(cfg) is False


# --- ledger authoritative-hold + consumption (codex review P1-1/P1-2/P1-3) ----

def test_reconcile_future_hold_short_circuits_main(cfg, monkeypatch):
    """P1-1: a dead window + future ledger alarm must short-circuit main() so
    no other wake path (e.g. an overdue floor via run_tick) fires early."""
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    monkeypatch.setattr(pacemaker_tick.config, "load", lambda: cfg)
    now = datetime.now(_tz(cfg))
    wake_state.set_next_wake_at(cfg, (now + timedelta(minutes=20)).isoformat())

    def _boom(*a, **k):
        raise AssertionError("run_tick must not run while a future ledger holds")
    monkeypatch.setattr(pacemaker_tick.integration, "run_tick", _boom)
    assert pacemaker_tick.main() == 0


def test_fire_dead_window_dry_run_consumes_ledger(cfg):
    """P1-2: a due-ledger fire in dry_run must replace next_wake_at with the
    freshly redrawn floor, not leave the stale due timestamp (else every
    subsequent tick re-fires the same reconcile wake)."""
    cfg["pacemaker"]["dry_run"] = True
    now = datetime.now(_tz(cfg))
    stale_due = now - timedelta(minutes=1)
    wake_state.set_next_wake_at(cfg, stale_due.isoformat())
    conn = db.connect(cfg)
    try:
        pacemaker_tick._fire_dead_window(conn, cfg, "ledger due, window dead")
    finally:
        conn.close()
    new_due = wake_state.get_next_wake_at(cfg)
    assert new_due is not None
    assert new_due != stale_due.isoformat()


def test_main_pause_short_circuits_before_night_close(cfg, monkeypatch):
    """P1-3: paused (DND) must hold night-close's wrap-up injection too — the
    pause check must run before _night_close, not just inside _reconcile."""
    monkeypatch.setattr(pacemaker_tick.config, "load", lambda: cfg)
    wake_state.set_paused(cfg, True)

    def _boom(*a, **k):
        raise AssertionError("_night_close must not run while paused")
    monkeypatch.setattr(pacemaker_tick, "_night_close", _boom)
    assert pacemaker_tick.main() == 0


# --- per-session _window_alive ------------------------------------------------

def test_window_alive_is_per_session(cfg, monkeypatch):
    """_window_alive must prove liveness via the recorded session's OWN tty
    (window._claude_on_session_tty), never the cwd fallback — so another claude
    window in cortex_home can't fake a dead session alive."""
    from cortex import wake, window
    wake_state.set_session_id(cfg, "SID-1")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    # find_claude_pid (with its cwd fallback) would return a pid for a foreign
    # window; if _window_alive used it, this would falsely read alive.
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 99999)
    monkeypatch.setattr(window, "_claude_on_session_tty", lambda c, sid: False)
    assert wake._window_alive(cfg) is False  # per-session check wins
    monkeypatch.setattr(window, "_claude_on_session_tty", lambda c, sid: True)
    assert wake._window_alive(cfg) is True


# --- ctl CLI ------------------------------------------------------------------

def test_ctl_pause_resume(cfg, monkeypatch, capsys):
    from cortex import ctl
    monkeypatch.setattr(ctl.config, "load", lambda: cfg)
    ctl.main(["pause"])
    assert wake_state.is_paused(cfg) is True
    ctl.main(["resume"])
    assert wake_state.is_paused(cfg) is False


def test_ctl_wake_clears_paused(cfg, monkeypatch):
    from cortex import ctl, window
    # Live-window path so cmd_wake returns before any machine-touching wake.
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
    monkeypatch.setattr(window, "append_wake_signal", lambda c, now: None)
    wake_state.set_paused(cfg, True)
    assert wake_state.is_paused(cfg) is True
    ctl.cmd_wake(cfg)
    assert wake_state.is_paused(cfg) is False


def test_ctl_sleep_dead_window_sets_ledger(cfg, monkeypatch):
    from cortex import ctl
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    msg = ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=True)
    assert wake_state.get_next_wake_at(cfg) is not None
    assert wake_state.load(cfg).get("rotated") is True
    assert "ledger set" in msg


def test_ctl_sleep_live_window_rotate_injects_rotate_arg(cfg, monkeypatch):
    """P2-1: `sleep --rotate` on a live window must generate a lie_down() call
    with rotate=true, not just add prose the session can miss."""
    from cortex import ctl, window
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
    captured = {}
    monkeypatch.setattr(window, "inject_prompt",
                        lambda c, text: captured.setdefault("text", text) or True)
    ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=True)
    assert "rotate=true" in captured["text"]
    assert "lie_down(" in captured["text"]


def test_ctl_sleep_live_window_no_rotate_omits_rotate_arg(cfg, monkeypatch):
    from cortex import ctl, window
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
    captured = {}
    monkeypatch.setattr(window, "inject_prompt",
                        lambda c, text: captured.setdefault("text", text) or True)
    ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=False)
    assert "rotate=true" not in captured["text"]


def test_ctl_wake_live_window_renders_fresh_note(cfg, monkeypatch):
    """P2-2: `wake` on a live window must render+write a fresh note before
    signalling, not append the bell onto a stale note from a previous wake."""
    from cortex import ctl, window
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
    calls = {"note": None}

    def fake_assemble_note(conn, cfg, now, **kw):
        return "FRESH NOTE TEXT"
    monkeypatch.setattr("cortex.wake.assemble_note", fake_assemble_note)
    monkeypatch.setattr(window, "write_note",
                        lambda c, text: calls.__setitem__("note", text))
    monkeypatch.setattr(window, "append_wake_signal", lambda c, now: None)
    ctl.cmd_wake(cfg)
    assert calls["note"] == "FRESH NOTE TEXT"


# --- ImportError guard --------------------------------------------------------

def test_tick_has_no_dangling_catchup_import():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "cortex" / "pacemaker_tick.py").read_text()
    assert "from cortex.pacemaker import catchup" not in src
    # the module imports cleanly (would ImportError at import time otherwise)
    import importlib
    importlib.reload(pacemaker_tick)
