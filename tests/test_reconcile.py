"""Ledger + tick reconcile + pause gating (schedule reliability fix).

Covers: next_wake_at write/clear, no night clamp (P8), the reconcile decision matrix
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
    c["paths"]["ny_db_pages"] = str(tmp_path / "ny")  # isolate symlinks.ensure_all
    c["paths"]["wake_timing_log"] = str(home / "wake_timing.log")  # not under cortex_home default
    c["paths"]["handoff_file"] = str(home / "handoff.md")
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


def test_lie_down_rotate_records_retired_sid(cfg):
    """lie_down(rotate=True) durably records the retiring session's sid (the
    transcript jsonl stem) at the same moment it sets the one-shot rotated
    flag — the belt-and-braces guard that outlives that flag being consumed
    by an unrelated later wake."""
    wake_state.set_awake(cfg, 1, "/t/retiring.jsonl")
    lie_down.lie_down(cfg, rotate=True, next_wake_min=30)
    assert wake_state.get_retired_sid(cfg) == "retiring"


def test_lie_down_no_rotate_leaves_retired_sid_untouched(cfg):
    wake_state.set_awake(cfg, 1, "/t/still-alive.jsonl")
    lie_down.lie_down(cfg, rotate=False, next_wake_min=30)
    assert wake_state.get_retired_sid(cfg) is None


# --- no night clamp (P8: gate-end clamp retired) ------------------------------

def test_arm_sentinel_no_night_clamp(cfg):
    """P8: the sentinel gate-end clamp is gone — a due time that once fell 'inside
    the old window' now arms at its REAL time (else the 120-360 roaming band would
    collapse to the gate end)."""
    tz = _tz(cfg)
    cfg["wake"]["sentinel"] = False  # no detached process in tests
    mid_night = datetime(2026, 7, 13, 2, 0, tzinfo=tz)
    effective = lie_down._arm_sentinel(cfg, mid_night)
    assert effective == mid_night  # unchanged, no clamp
    ledger = wake_state.get_next_wake_at(cfg)
    assert ledger is not None and "02:00" in ledger


def test_lie_down_reports_real_next_wake(cfg):
    """lie_down()'s reported next_wake matches the ledger exactly (no clamp)."""
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=20)
    ledger = wake_state.get_next_wake_at(cfg)
    assert ledger is not None and r["next_wake"] is not None
    assert r["next_wake"] in ledger  # HH:MM substring of the ISO ledger


def test_lie_down_night_mode_sets_flag_and_night_band(cfg):
    """lie_down(mode='night') sets the persistent flag and clamps N to the night
    band [120, 360]."""
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=10, mode="night")  # 10 < 120 -> clamps up
    assert r["mode"] == "night"
    assert r["rotated"] is True  # night forces rotate
    assert wake_state.is_night_mode(cfg) is True
    ledger = wake_state.get_next_wake_at(cfg)
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    due = _dt.fromisoformat(ledger)
    tz = ZoneInfo(cfg["core"]["timezone"])
    delta_min = (due - _dt.now(tz)).total_seconds() / 60.0
    assert 119 <= delta_min <= 361  # ~120 (clamped up from 10)


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


def test_fire_dead_window_accidental_close_always_fresh(cfg, monkeypatch):
    """P17: reconcile's accidental-close fire shares the fresh-only choke point
    (_window_wake -> _fresh_with_catchup) — a retired_sid or any other dead-
    window state always fresh-spawns via _spawn_wake, never a resume."""
    from cortex import transcript, wake, window
    cfg["gates"]["night"] = {"start": "23:00", "end": "23:00", "cap": 0}  # disabled
    cfg["pacemaker"]["dry_run"] = False
    cfg["wake"]["mode"] = "window"
    wake_state.set_session_id(cfg, "SID-1")
    wake_state.update(cfg, transcript="/t/retired-sid.jsonl")
    wake_state.set_retired_sid(cfg, "/t/retired-sid.jsonl")
    monkeypatch.setattr(window, "is_running", lambda: False)  # dead resident
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, c, now, **kw:
                        captured.update(called=True) or {"mode": "window"})
    conn = db.connect(cfg)
    try:
        pacemaker_tick._fire_dead_window(conn, cfg, "accidental close of awake window")
    finally:
        conn.close()
    assert captured.get("called") is True  # fresh spawn, no resume path exists


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
    cfg["gates"]["night"] = {"start": "23:00", "end": "23:00", "cap": 0}  # disabled
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


def test_fire_dead_window_night_cap_gated_holds_ledger(cfg):
    """P8: a due-ledger fire while the night flag is set AND the per-night cap is
    exhausted must HOLD (night-cap gate disallows) and leave next_wake_at
    UN-consumed, so reconcile retries once the flag clears / a new night starts."""
    cfg["night"]["cap"] = 1
    wake_state.update(cfg, mode="night")
    # Persist a pacemaker state already at cap for this night.
    conn0 = db.connect(cfg)
    try:
        from cortex.pacemaker import integration
        from cortex.pacemaker.core import PacemakerState
        integration.save_state(conn0, PacemakerState(
            night_cap_key="night", night_wake_count=1))
    finally:
        conn0.close()
    now = datetime.now(_tz(cfg))
    stale_due = now - timedelta(minutes=1)
    wake_state.set_next_wake_at(cfg, stale_due.isoformat())
    conn = db.connect(cfg)
    try:
        msg = pacemaker_tick._fire_dead_window(conn, cfg, "ledger due, window dead")
    finally:
        conn.close()
    assert "gated" in msg.lower()
    assert wake_state.get_next_wake_at(cfg) == stale_due.isoformat()  # untouched


def test_fire_dead_window_daily_budget_gated_holds_ledger(cfg):
    """P1-B: a due-ledger fire after daily budget exhaustion must also HOLD."""
    from zoneinfo import ZoneInfo
    cfg["gates"]["night"] = {"start": "23:00", "end": "23:00", "cap": 0}  # disabled
    cfg["gates"]["daily_budget"] = {"tokens": 100}
    # _fire_dead_window reads the REAL wall clock (integration._now) for the
    # gate check, so the "finished window" row must land in TODAY's local
    # window (local midnight -> now), matching test_daily_budget_gates_floor.
    now = pacemaker_tick.integration._now(cfg)
    midnight = now.replace(hour=0, minute=1, second=0, microsecond=0)
    day = midnight.astimezone(ZoneInfo("UTC"))
    stale_due = now - timedelta(minutes=1)
    wake_state.set_next_wake_at(cfg, stale_due.isoformat())
    conn = db.connect(cfg)
    try:
        # A FINISHED window (peak over cap, then a lower row closes it) puts
        # Cortex Today over the cap — same pattern as
        # test_integration.test_daily_budget_gates_floor.
        conn.executemany(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)",
            [(day.isoformat(), 200), ((day + timedelta(minutes=5)).isoformat(), 3)])
        conn.commit()
        msg = pacemaker_tick._fire_dead_window(conn, cfg, "ledger due, window dead")
    finally:
        conn.close()
    assert "gated" in msg.lower()
    assert wake_state.get_next_wake_at(cfg) == stale_due.isoformat()  # untouched


def test_main_pause_short_circuits_before_reconcile(cfg, monkeypatch):
    """Paused (DND) holds everything: main() returns 0 before running reconcile /
    the tick, so no wake path fires."""
    monkeypatch.setattr(pacemaker_tick.config, "load", lambda: cfg)
    wake_state.set_paused(cfg, True)

    def _boom(*a, **k):
        raise AssertionError("_reconcile must not run while paused")
    monkeypatch.setattr(pacemaker_tick, "_reconcile", _boom)
    assert pacemaker_tick.main() == 0


def test_main_normal_tick_dry_run_wake_sets_ledger(cfg, monkeypatch):
    """Follow-up to P1-2: main()'s normal-tick dry-run wake path must also
    write the redrawn floor into next_wake_at, not just log-only advance the
    in-memory floor — else the ledger goes stale here too."""
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    monkeypatch.setattr(pacemaker_tick.config, "load", lambda: cfg)
    cfg["pacemaker"]["dry_run"] = True
    now = datetime.now(_tz(cfg))
    decision = {"wake": True, "reasons": [], "gated_by": [], "explanation": "test"}
    monkeypatch.setattr(pacemaker_tick.integration, "run_tick",
                        lambda conn, c, now=None: decision)
    assert wake_state.get_next_wake_at(cfg) is None
    assert pacemaker_tick.main() == 0
    assert wake_state.get_next_wake_at(cfg) is not None


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


def test_ctl_wake_dormant_signal_clears_paused(cfg, monkeypatch):
    """Remote wake of a live-but-dormant resident lifts DND (ct-pause documents
    /ct-wake as its exit). The caller never takes office."""
    from cortex import ctl, wake
    wake_state.update(cfg, cortex_resident_pid=4321)  # live foreign resident, dormant
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign
    monkeypatch.setattr(wake, "run_wake", lambda *a, **k: {"mode": "window"})
    wake_state.set_paused(cfg, True)
    assert wake_state.is_paused(cfg) is True
    ctl.cmd_wake(cfg)
    assert wake_state.is_paused(cfg) is False


def test_ctl_wake_dead_resident_reports_never_spawns(cfg, monkeypatch):
    """Resident dead (no recorded pid / not alive) -> report death + diagnostics
    hint, DO NOT spawn (spawn authority is exclusively the pacemaker's). The
    caller never takes office, never sets awake, never writes registration."""
    from cortex import ctl, wake
    # no cortex_resident_pid recorded -> resident_alive False -> dead branch
    wake_state.set_retired_sid(cfg, "some-other-session-from-ages-ago")
    wake_state.set_next_wake_at(cfg, "2099-01-01T00:00:00+10:00")
    spawned = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1))
    line = ctl.cmd_wake(cfg)
    assert spawned["n"] == 0  # never spawns
    assert wake_state.is_awake(cfg) is False  # no take-office
    assert "cortex_claude_sid" not in wake_state.load(cfg)
    assert wake_state.get_next_wake_at(cfg) == "2099-01-01T00:00:00+10:00"  # untouched
    assert "{backup_hint}" not in line  # template placeholder substituted
    assert line.startswith(cfg["wake"]["ctl_wake_dead_text"].split("{")[0])


def test_ctl_wake_live_awake_resident_on_duty_zero_side_effects(cfg, monkeypatch):
    """Live resident, already awake -> on-duty text, zero side effects. The caller
    is an ordinary remote and never touches wake_state."""
    from cortex import ctl, wake
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign live resident
    wake_state.set_awake(cfg, 1, None)
    spawned = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1))
    before = wake_state.load(cfg)
    line = ctl.cmd_wake(cfg)
    assert line == cfg["wake"]["ctl_wake_awake_text"]
    assert spawned["n"] == 0  # awake -> no signal
    assert wake_state.load(cfg) == before  # zero side effects


def test_ctl_wake_live_dormant_resident_sends_ear_signal(cfg, monkeypatch):
    """Live but DORMANT resident -> send a wake signal NOW via the pacemaker fire
    path (forced ctl decision through run_wake). The caller never takes office."""
    from cortex import ctl, wake
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign live resident
    calls = {}
    monkeypatch.setattr(wake, "run_wake",
                        lambda conn, c, decision, **k: calls.setdefault("decision", decision))
    assert wake_state.is_awake(cfg) is False
    line = ctl.cmd_wake(cfg)
    assert line == cfg["wake"]["ctl_wake_signal_text"]
    assert calls["decision"]["wake"] is True
    assert calls["decision"]["wake_reasons"] == "ctl"
    assert wake_state.is_awake(cfg) is False  # caller never takes office


def test_ctl_sleep_dead_window_sets_ledger(cfg):
    from cortex import ctl
    msg = ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=True)
    assert wake_state.get_next_wake_at(cfg) is not None
    assert wake_state.load(cfg).get("rotated") is True
    assert "ledger set" in msg


def test_ctl_sleep_gates_on_awake_not_liveness(cfg):
    """P2-A: a resident window can be alive-but-dormant (asleep). cmd_sleep
    must gate the live-window injection on the awake marker, not liveness —
    else the requested minutes/rotate silently drop via claim_lie_down's
    'not awake' no-op."""
    from cortex import ctl
    wake_state.set_session_id(cfg, "SID-1")  # a resident session exists
    # awake marker NOT set -> even if the window were alive, must fall to the
    # ledger-direct path, not the injection path.
    msg = ctl.cmd_sleep(cfg, until=None, minutes=15, rotate=False)
    assert "ledger set" in msg
    assert wake_state.get_next_wake_at(cfg) is not None


def test_ctl_sleep_live_window_rotate_delivers_marker_with_args(cfg, monkeypatch):
    """P2-1: `sleep --rotate` on a live+awake window delivers the covert CTL
    marker carrying mins + rotate=true (the body renders marrow-side from these
    args). Only the marker + args reach the window, never the instruction body."""
    from cortex import ctl, window
    wake_state.set_awake(cfg, 1, None)
    captured = {}
    monkeypatch.setattr(window, "deliver_covert_marker",
                        lambda c, line: captured.setdefault("line", line) or "bell")
    ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=True)
    assert "[CTL]" in captured["line"]
    assert "mins=30" in captured["line"]
    assert "rotate=true" in captured["line"]
    assert "lie_down(" not in captured["line"]  # body not on screen


def test_ctl_sleep_live_window_no_rotate_omits_rotate_true(cfg, monkeypatch):
    from cortex import ctl, window
    wake_state.set_awake(cfg, 1, None)
    captured = {}
    monkeypatch.setattr(window, "deliver_covert_marker",
                        lambda c, line: captured.setdefault("line", line) or "bell")
    ctl.cmd_sleep(cfg, until=None, minutes=30, rotate=False)
    assert "rotate=false" in captured["line"]
    assert "rotate=true" not in captured["line"]



# --- ImportError guard --------------------------------------------------------

def test_tick_has_no_dangling_catchup_import():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "cortex" / "pacemaker_tick.py").read_text()
    assert "from cortex.pacemaker import catchup" not in src
    # the module imports cleanly (would ImportError at import time otherwise)
    import importlib
    importlib.reload(pacemaker_tick)
