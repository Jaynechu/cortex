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


# --- rotate: spawn authority is the sentinel/pacemaker only (no direct spawn) --

def test_lie_down_rotate_never_spawns_directly(cfg, monkeypatch):
    """Spawn authority belongs exclusively to the sentinel/pacemaker chain:
    lie_down(rotate=True) must NOT spawn a successor itself. It only sets the
    one-shot rotated flag and arms the sentinel at the requested time — the
    sentinel (or the 60s pacemaker tick fallback) fires the fresh successor."""
    from cortex import wake
    fired = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    wake_state.set_awake(cfg, 1, "/t/retiring.jsonl")
    r = lie_down.lie_down(cfg, rotate=True, next_wake_min=30)
    assert r["rotated"] is True  # rotate flag set
    assert fired["n"] == 0  # nothing spawned from lie_down
    assert wake_state.load(cfg).get("rotated") is True  # flag left for the sentinel
    assert wake_state.get_next_wake_at(cfg) is not None  # sentinel/ledger armed


def test_lie_down_no_rotate_never_spawns_successor(cfg, monkeypatch):
    """A plain (non-rotate) sleep never spawns a successor — the resident stays."""
    from cortex import wake
    fired = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    wake_state.set_awake(cfg, 1, "/t/alive.jsonl")
    lie_down.lie_down(cfg, rotate=False, next_wake_min=30)
    assert fired["n"] == 0


def test_run_wake_two_concurrent_spawn_entrants_only_one_spawns(cfg, monkeypatch, tmp_path):
    """07-20 live race repro: a SIGKILLed resident (simulated crash, no rotate) +
    a concurrent ctl wake both pass the "no resident" check before either used to
    spawn (two unlocked steps) -> two identical windows landed. Two threads both
    call wake.run_wake through the real window/spawn branch (_window_wake ->
    _spawn_wake, with only window.respawn/watchdog.spawn/osascript-adjacent calls
    stubbed) against the SAME on-disk state dir; window.respawn sleeps briefly so
    both threads are inside the classify-then-spawn window at the same time if
    unlocked. Exactly one must actually spawn; the loser, re-checking _window_alive
    under the serialization lock, must see the winner's now-live window and skip."""
    import threading
    import time as _time
    from pathlib import Path
    from cortex import transcript, wake, watchdog, window

    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    spawn_calls = {"n": 0}
    lock_for_calls = threading.Lock()
    # A REAL file (unlike a bare "/t/new.jsonl" string) so transcript.mtime's
    # p.stat() (called by the loser's "ear" branch) never raises FileNotFoundError
    # in the background thread -- mirrors production, where the winner's spawn
    # really creates this file before _wait_new_transcript returns its path.
    new_transcript_path = tmp_path / "new.jsonl"
    new_transcript_path.write_text("{}")
    NEW_TRANSCRIPT = str(new_transcript_path)

    def _respawn_stub(c, initial_prompt=None, resume_sid=None):
        with lock_for_calls:
            spawn_calls["n"] += 1
            # The winner records its new session; from here _window_alive reads True.
            wake_state.set_session_id(cfg, "new-iterm-sid")
        _time.sleep(0.05)  # widen the race window
        return "new-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn_stub)
    # _window_alive = a session is recorded (the winner's spawn set it). Under the
    # serialization lock the loser sees it live and skips.
    monkeypatch.setattr(wake, "_window_alive",
                        lambda c: bool(wake_state.get_session_id(c)))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: NEW_TRANSCRIPT)
    # transcript.newest must agree with the recorded hint the winner's commit
    # just wrote (both resolve to the SAME real file in production -- the spawn
    # actually creates it before _wait_new_transcript returns it). Without this,
    # the loser's in-lock classification (now running strictly AFTER the winner,
    # since classify+dispatch is fully serialized -- Fix 1) sees a recorded
    # transcript hint the on-disk "newest" lookup never confirms, misreads that
    # as a /clear (prev != cur) and classifies "fresh" -> a SECOND spawn.
    monkeypatch.setattr(transcript, "newest", lambda c: Path(NEW_TRANSCRIPT))
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    # Fix 1 (codex adversarial-review): classification now runs INSIDE the spawn
    # lock, so the loser's classification happens AFTER it acquires the lock --
    # i.e. after the winner already recorded a session id above. Its
    # classification then reaches window.is_running()/_session_alive (a real
    # osascript call, blocked by conftest's process guard) instead of the
    # no-sid-yet short-circuit it hit under the old classify-before-lock
    # ordering. Stub these so the loser's re-classification stays in-process.
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)

    def _fire():
        conn = db.connect(cfg)
        try:
            now = datetime.now(_tz(cfg))
            decision = {"wake": True, "reasons": [], "gated_by": [],
                        "wake_reasons": "test",
                        "explanation": "concurrent entrant"}
            wake.run_wake(conn, cfg, decision, now=now)
        finally:
            conn.close()

    t1 = threading.Thread(target=_fire)
    t2 = threading.Thread(target=_fire)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert spawn_calls["n"] == 1  # exactly one entrant actually spawned a window
    # The winner recorded its session before releasing the lock, so the loser's
    # _window_alive recheck saw a live window and skipped: only one window.respawn
    # call ever fired, so at most one real iTerm window exists.


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
    # Adoption runs before any dead-window fire; with no manual window to adopt
    # (the default) it must be a no-op so the fire/hold matrix is exercised.
    monkeypatch.setattr(pacemaker_tick, "_adopt_manual_window", lambda cfg: None)
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


def test_fire_dead_window_accidental_close_resumes(cfg, monkeypatch):
    """Accidental close of an awake window (no rotate flag) with a recoverable
    session -> RESUME the same conversation (conversation = identity), not a
    fresh spawn. _spawn_wake is called with resume=True."""
    from cortex import transcript, wake, window
    cfg["gates"]["night"] = {"start": "23:00", "end": "23:00", "cap": 0}  # disabled
    cfg["pacemaker"]["dry_run"] = False
    cfg["wake"]["mode"] = "window"
    wake_state.set_session_id(cfg, "SID-1")
    wake_state.update(cfg, transcript="/t/live-sid.jsonl")  # recoverable, not retired
    monkeypatch.setattr(window, "is_running", lambda: False)  # dead resident
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, c, now, resume=False, **kw:
                        captured.update(resume=resume) or {"mode": "window"})
    conn = db.connect(cfg)
    try:
        pacemaker_tick._fire_dead_window(conn, cfg, "accidental close of awake window")
    finally:
        conn.close()
    assert captured.get("resume") is True  # same conversation resumed, not fresh


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
    monkeypatch.setattr(pacemaker_tick, "_adopt_manual_window", lambda cfg: None)
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
    monkeypatch.setattr(pacemaker_tick, "_adopt_manual_window", lambda cfg: None)
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
