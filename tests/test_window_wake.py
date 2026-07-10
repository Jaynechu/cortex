"""B3v tests: deterministic logic for wake_state, transcript parsing, and
lie_down (self-schedule clearing + token recording). No iTerm/osascript here —
window control is verified live. Uses a temp cortex_home + temp DB."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, db, lie_down, transcript, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dbfile = tmp_path / "marrow.db"
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(dbfile)
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


# --- wake_state ---------------------------------------------------------------

def test_wake_state_roundtrip(cfg):
    assert wake_state.is_awake(cfg) is False
    wake_state.set_session_id(cfg, "SID-1")
    assert wake_state.get_session_id(cfg) == "SID-1"
    wake_state.set_awake(cfg, 42, "/x/y.jsonl")
    d = wake_state.load(cfg)
    assert d["awake"] is True and d["wake_log_id"] == 42
    assert d["session_id"] == "SID-1"  # awake marker preserves other keys
    wake_state.clear_awake(cfg)
    assert wake_state.is_awake(cfg) is False
    assert wake_state.get_session_id(cfg) == "SID-1"  # session id survives


# --- transcript ---------------------------------------------------------------

def test_munge_matches_claude_dir():
    assert transcript._munge("/Users/x/.config/marrow/cortex") == \
        "-Users-x--config-marrow-cortex"


def test_window_tokens_last_usage(cfg):
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    rows = [
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 1, "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 2, "output_tokens": 3}}},
        {"type": "user", "message": {"role": "user"}},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 5, "cache_read_input_tokens": 90_000,
            "cache_creation_input_tokens": 1_000, "output_tokens": 500}}},
    ]
    (d / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert transcript.window_tokens(cfg) == 5 + 90_000 + 1_000 + 500


def test_window_tokens_no_transcript(cfg):
    assert transcript.window_tokens(cfg) == 0
    assert transcript.mtime(cfg) is None


def test_net_tokens_sums_creation_plus_output(cfg):
    """net_tokens = SUM over every assistant usage of (cache_creation + output);
    excludes cache_read (hit ~free) and plain input (mostly cached)."""
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    rows = [
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 10, "cache_read_input_tokens": 5_000,
            "cache_creation_input_tokens": 200, "output_tokens": 50}}},
        {"type": "user", "message": {"role": "user"}},
        {"type": "assistant", "message": {"usage": {
            "input_tokens": 20, "cache_read_input_tokens": 90_000,
            "cache_creation_input_tokens": 1_000, "output_tokens": 500}}},
    ]
    (d / "s.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    # (200+50) + (1000+500) = 1750 ; window_tokens (last-msg occupancy) differs
    assert transcript.net_tokens(cfg) == 1_750
    assert transcript.window_tokens(cfg) == 20 + 90_000 + 1_000 + 500


def test_net_tokens_no_transcript(cfg):
    assert transcript.net_tokens(cfg) == 0


# --- lie_down: self-schedule clearing ----------------------------------------

def test_clear_due_self_schedule(cfg):
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=5)).isoformat()
    future = (now + timedelta(hours=2)).isoformat()
    p = config.self_schedule_path(cfg)
    p.write_text(json.dumps([
        {"due_at": past, "intent": "gone"},
        {"due_at": future, "intent": "kept"},
    ]))
    removed = lie_down._clear_due_self_schedule(cfg)
    assert removed == 1
    left = json.loads(p.read_text())
    assert [x["intent"] for x in left] == ["kept"]


def test_clear_due_self_schedule_naive_local(cfg):
    """Offset-free (naive) due_at is read as Australia/Melbourne local time."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(cfg["core"]["timezone"])
    now_local = datetime.now(tz)
    past_naive = (now_local - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
    future_naive = (now_local + timedelta(hours=4)).replace(tzinfo=None).isoformat()
    p = config.self_schedule_path(cfg)
    p.write_text(json.dumps([
        {"due_at": past_naive, "intent": "past-local"},
        {"due_at": future_naive, "intent": "future-local"},
    ]))
    assert lie_down._clear_due_self_schedule(cfg) == 1
    assert [x["intent"] for x in json.loads(p.read_text())] == ["future-local"]


def test_clear_due_self_schedule_bare_dict(cfg):
    """A bare dict (not wrapped in a list) is tolerated: treated as one entry,
    and the file is always rewritten as a list."""
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=5)).isoformat()
    p = config.self_schedule_path(cfg)
    p.write_text(json.dumps({"due_at": past, "intent": "gone"}))
    removed = lie_down._clear_due_self_schedule(cfg)
    assert removed == 1
    left = json.loads(p.read_text())
    assert left == []


# --- lie_down: token recording into ct_wake_log ------------------------------

def test_window_wake_alive_uses_ear(cfg, monkeypatch):
    """Alive resident window: _window_wake writes the note file, appends ONE
    bell signal line (no respawn, no note-as-prompt), captures the wake row id,
    sets the awake marker, and lights the watchdog — verified without osascript."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "dispatch"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: calls.setdefault("respawn", True))
    monkeypatch.setattr(
        window, "append_wake_signal",
        lambda c, now: calls.setdefault("signal", True))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: True)
    monkeypatch.setattr(watchdog, "spawn", lambda c: calls.setdefault("watchdog", True))

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "NOTE-BODY", _dt.now(timezone.utc))
    conn.close()
    assert res == {"mode": "window", "session_id": None, "text": None}
    assert "respawn" not in calls               # live window is not respawned
    assert calls["signal"] is True              # bell appended once
    assert calls["watchdog"] is True
    # note file written with the note body
    assert wake_state.wakeup_note_path(cfg).read_text() == "NOTE-BODY"
    d = wake_state.load(cfg)
    assert d["awake"] is True and d["wake_log_id"] == wid


def test_window_wake_respawn_delivers_note_as_prompt(cfg, monkeypatch):
    """respawn=True (rotate/rebirth) spawns a FRESH window with the emoji +
    bell-marker first prompt baked in (fresh_initial_prompt) — no signal
    append, no notification (silent wake) — and sets the awake marker +
    watchdog. The marker in the baked prompt is what makes marrow's hook
    inject the full wakeup note into the new window."""
    from cortex import transcript, wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "respawn"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]

    calls = {}
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: calls.setdefault("prompt", initial_prompt))
    assert not hasattr(window, "spawn_greeting")  # greeting mechanism removed
    monkeypatch.setattr(window, "append_wake_signal",
                        lambda c, now: calls.setdefault("signal", True))
    # New session jsonl appears promptly (skip the real 8s poll).
    monkeypatch.setattr(transcript, "newest",
                        lambda c: __import__("pathlib").Path("/t/new.jsonl"))
    monkeypatch.setattr(watchdog, "spawn", lambda c: calls.setdefault("watchdog", True))

    from datetime import datetime as _dt
    now = _dt.now(timezone.utc)
    res = wake._window_wake(conn, cfg, "N", now, respawn=True)
    conn.close()
    assert res["mode"] == "window"
    assert calls["prompt"] == window.fresh_initial_prompt(cfg, now)
    assert window.wake_prompt(cfg) in calls["prompt"]           # emoji present
    assert cfg["wake"].get("wake_signal_marker", "[CORTEX-WAKE]") in calls["prompt"]  # bell marker present
    assert "signal" not in calls                # fresh path never appends a signal
    assert calls["watchdog"] is True
    d = wake_state.load(cfg)
    assert d["awake"] is True and d["wake_log_id"] == wid


def test_window_wake_ear_miss_alive_types_rearm_not_respawn(cfg, monkeypatch):
    """Ladder 2a: ear miss on an ALIVE window -> type the rearm bell line (no
    respawn), poll again; land -> ear wake. No fresh window is spawned."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "rearm"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]

    calls = {"respawn": 0, "signal": 0, "rearm": 0}
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: calls.__setitem__("respawn", calls["respawn"] + 1))
    monkeypatch.setattr(window, "append_wake_signal",
                        lambda c, now: calls.__setitem__("signal", calls["signal"] + 1))
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now: calls.__setitem__("rearm", calls["rearm"] + 1) or True)
    # first poll (original signal) misses, second poll (after rearm) lands
    landings = iter([False, True])
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: next(landings))
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["respawn"] == 0   # alive window is NOT respawned
    assert calls["signal"] == 1    # original ear bell once
    assert calls["rearm"] == 1     # rearm typed once
    assert wake_state.load(cfg)["awake"] is True and wake_state.load(cfg)["wake_log_id"] == wid


def test_window_wake_ear_miss_dead_respawns_with_catchup(cfg, monkeypatch):
    """Ladder 2b: ear miss AND claude dead -> respawn fresh. The dead window left
    no handoff -> the rebuilt note carries the died_no_handoff catchup line."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "dead"))
    conn.commit()

    calls = {"respawn": 0, "rearm": 0}
    # alive on the initial gate, dead when the ladder re-checks
    alive = iter([True, False])
    monkeypatch.setattr(wake, "_window_alive", lambda c: next(alive))
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: calls.__setitem__("respawn", calls["respawn"] + 1))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(window, "append_wake_signal", lambda c, now: None)
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now: calls.__setitem__("rearm", calls["rearm"] + 1))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: False)  # never lands
    monkeypatch.setattr(wake, "_handoff_written_this_window", lambda c: False)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["respawn"] == 1   # dead window respawned exactly once
    assert calls["rearm"] == 0     # dead window is not re-typed
    note_text = wake_state.wakeup_note_path(cfg).read_text()
    assert "died without a handoff" in note_text  # catchup line baked into the note


def test_window_wake_falls_back_on_window_error(cfg, monkeypatch):
    """An osascript/iTerm failure (WindowError) in the respawn path -> None so
    the caller drops to the headless fallback; awake marker stays off."""
    from cortex import wake, window

    def boom(c, initial_prompt=None, resume_sid=None):
        raise window.WindowError("no iterm")
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead -> fresh path
    monkeypatch.setattr(window, "respawn", boom)
    from datetime import datetime as _dt
    assert wake._window_wake(None, cfg, "x", _dt.now(timezone.utc)) is None
    assert wake_state.is_awake(cfg) is False


def test_lie_down_records_tokens(cfg):
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "test wake"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    # seed transcript so window_tokens > 0
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 23}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg, force_slept="timeout")
    assert r["tokens"] == 123
    conn = db.connect(cfg)
    row = conn.execute("SELECT tokens, force_slept FROM ct_wake_log WHERE id=?",
                       (wid,)).fetchone()
    conn.close()
    assert row["tokens"] == 123 and row["force_slept"] == "timeout"
    assert wake_state.is_awake(cfg) is False  # marker cleared


def test_store_window_tokens_reaches_budget_line(cfg):
    """store_window_tokens publishes to ct_pacemaker_state; note reads it back
    (Budget line 'net Xk'). Survives lie_down's own floor-redraw save_state."""
    from cortex import note
    from cortex.pacemaker import integration

    conn = db.connect(cfg)
    try:
        integration.store_window_tokens(conn, 88_000)
        assert note._window_tokens(conn) == 88_000
        # a later floor-redraw save must NOT wipe it out of order
        integration.lie_down(conn, cfg)
        integration.store_window_tokens(conn, 90_000)
        assert note._window_tokens(conn) == 90_000
    finally:
        conn.close()


# --- signal-file ear ----------------------------------------------------------

def test_append_wake_signal_line_format(cfg):
    """append_wake_signal writes exactly one BELL line: '<marker> HH:MM'. No note
    body, no read errand — the marker alone is what the marrow hook detects to
    inject the full note."""
    from datetime import datetime as _dt

    from cortex import window

    now = _dt(2026, 7, 11, 9, 5, tzinfo=timezone.utc)
    window.append_wake_signal(cfg, now)
    text = config.wake_signal_log_path(cfg).read_text().strip()
    assert text == "[CORTEX-WAKE] 09:05"


def test_append_wake_signal_appends_not_overwrites(cfg):
    """Multiple signals accumulate (the ear tails the file)."""
    from datetime import datetime as _dt

    from cortex import window

    now = _dt(2026, 7, 11, 9, 5, tzinfo=timezone.utc)
    window.append_wake_signal(cfg, now)
    window.append_wake_signal(cfg, now)
    lines = config.wake_signal_log_path(cfg).read_text().strip().splitlines()
    assert len(lines) == 2


def test_wake_signal_line_rearm_suffix(cfg):
    """wake_signal_line(rearm=True) appends the ear-died suffix (ladder 2a)."""
    from datetime import datetime as _dt

    from cortex import window

    now = _dt(2026, 7, 11, 9, 5, tzinfo=timezone.utc)
    assert window.wake_signal_line(cfg, now) == "[CORTEX-WAKE] 09:05"
    assert window.wake_signal_line(cfg, now, rearm=True) == \
        "[CORTEX-WAKE] 09:05 (ear died — rearm)"


# --- wakeup note baked into the launch command --------------------------------

def test_wake_prompt_is_emoji_only(cfg):
    """wake_prompt returns the configured emoji only — the marrow hook injects
    the full note on it. No note path substitution."""
    from cortex import window

    assert window.wake_prompt(cfg) == "☀️"
    cfg["wake"]["wake_prompt"] = "GO"
    assert window.wake_prompt(cfg) == "GO"


def test_fresh_initial_prompt_composes_emoji_and_bell_marker(cfg):
    """fresh_initial_prompt bakes '<wake_prompt> <wake_signal_line>' — the
    baked first prompt of a fresh/resumed window must carry the same bell
    marker as the ear so the marrow hook detects it and injects the note."""
    from datetime import datetime, timezone
    from cortex import window

    now = datetime(2026, 7, 10, 0, 55, tzinfo=timezone.utc)
    prompt = window.fresh_initial_prompt(cfg, now)
    assert prompt == "☀️ [CORTEX-WAKE] 00:55"
    assert prompt == f"{window.wake_prompt(cfg)} {window.wake_signal_line(cfg, now)}"

    cfg["wake"]["wake_prompt"] = "GO"
    cfg["wake"]["wake_signal_marker"] = "[WAKE]"
    assert window.fresh_initial_prompt(cfg, now) == "GO [WAKE] 00:55"


def test_launch_command_bakes_initial_prompt(cfg):
    """launch_command bakes a non-empty initial_prompt as claude's first
    positional prompt (single-quoted) so a fresh window acts with zero typing."""
    from cortex import window

    cmd = window.launch_command(cfg, "Read /x/note.md — act on it")
    assert cmd.rstrip().endswith("'Read /x/note.md — act on it'")
    assert "arm" not in cmd  # no arm mechanism left


def test_launch_command_no_prompt_when_none(cfg):
    """No initial prompt -> no trailing prompt arg, window still launches."""
    from cortex import window

    cmd = window.launch_command(cfg)
    assert cmd.rstrip().endswith("--dangerously-skip-permissions")


def test_arm_mechanism_retired(cfg):
    """The arm-prompt boot mechanism is fully gone."""
    from cortex import config as _config, window

    assert not hasattr(window, "arm_prompt")
    assert not hasattr(_config, "arm_prompt_path")


def test_spawn_greeting_mechanism_removed():
    """The spawn notification is gone entirely — fresh windows wake silently,
    the emoji prompt is the only trace. No greeting / _notify / display
    notification anywhere in window.py."""
    import inspect

    from cortex import window

    assert not hasattr(window, "spawn_greeting")
    assert not hasattr(window, "_notify")
    assert "display notification" not in inspect.getsource(window)


def test_no_notification_config_key():
    """spawn_greeting config key dropped; wake_prompt defaults to the emoji."""
    from pathlib import Path

    from cortex import config

    c = config.load(path=Path("/no-such.toml"))
    assert "spawn_greeting" not in c["wake"]
    assert c["wake"]["wake_prompt"] == "☀️"


def test_spawn_wake_records_new_transcript_not_stale(cfg, monkeypatch):
    """P0 regression: _spawn_wake must NOT record the pre-spawn (OLD session)
    transcript. Before the fix it called transcript.newest() right after respawn
    — the new claude has not written its jsonl yet, so it recorded the PREVIOUS
    session's path; _window_rotated then saw a mismatch every tick and respawned
    forever. After the fix it polls for the NEW jsonl (or None on timeout) and
    records that, so a second consecutive wake on the alive window takes the ear
    path, not respawn.

    Timing model (the crux): at the instant respawn() returns, the new session
    jsonl does NOT exist yet — the new claude writes it only once it starts its
    turn. So the FIRST transcript.newest() after respawn still returns the OLD
    file; the NEW file appears on a LATER poll. The old code (single newest()
    right after respawn) recorded OLD; the fixed poll waits for NEW. Modelled
    with a stateful newest() stub: OLD for the first N reads, then NEW."""
    from datetime import datetime as _dt

    from cortex import transcript, wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "p0"))
    conn.commit()

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    old = tdir / "OLD.jsonl"
    new = tdir / "NEW.jsonl"

    # newest() returns OLD until the new session's jsonl "appears" on the 3rd
    # read (as it does in production, a beat after respawn). The pre-spawn read
    # + immediate post-spawn read both see OLD; only a poll finds NEW.
    reads = {"n": 0}

    def stub_newest(c):
        reads["n"] += 1
        return new if reads["n"] >= 3 else old

    monkeypatch.setattr(transcript, "newest", stub_newest)
    monkeypatch.setattr(window, "respawn", lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    # Zero the poll sleep so the test does not actually wait.
    monkeypatch.setattr(wake.time, "sleep", lambda s: None)

    wake._spawn_wake(conn, cfg, _dt.now(timezone.utc))
    conn.close()

    recorded = wake_state.load(cfg)["transcript"]
    assert recorded == str(new)          # NEW session, not the stale OLD path
    assert recorded != str(old)          # old timing recorded OLD here — the bug

    # Second wake: window alive, same NEW transcript, no rotate flag -> ear path.
    wake_state.set_session_id(cfg, "sid-new")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)
    assert wake._window_rotated(cfg) is False  # no respawn loop


def test_wait_new_transcript_prev_none_rejects_stale_mtime(cfg, monkeypatch):
    """Second symptom of the same P0 timing bug: when prev_path is None (the
    common case, since the 8s spawn poll routinely times out before the 30s+
    transcript-creation), `cur_s != prev_path` is trivially true for ANY
    existing jsonl — the old code returned the first stale file it found on
    the very first poll iteration, bypassing the fresh_mtime check entirely
    (live-confirmed: wake_state recorded an old session's uuid instead of the
    new window's). With prev_path None, only fresh_mtime (mtime >= spawn_ts)
    may accept a candidate; a stale file (mtime < spawn_ts) must be rejected
    for the whole poll window, yielding None on timeout."""
    from cortex import transcript, wake

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    stale = tdir / "stale-session.jsonl"
    stale.write_text("{}")
    spawn_ts = stale.stat().st_mtime + 100  # spawn started AFTER the stale file's mtime

    monkeypatch.setattr(wake.time, "sleep", lambda s: None)  # no real waiting
    result = wake._wait_new_transcript(cfg, None, spawn_ts)
    assert result is None  # stale file must never be accepted when prev is None


def test_wait_new_transcript_prev_none_accepts_fresh_mtime(cfg, monkeypatch):
    """Companion case: prev_path None but the jsonl's mtime IS >= spawn_ts (a
    genuinely new file) -> accepted immediately."""
    from cortex import transcript, wake

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    fresh = tdir / "fresh-session.jsonl"
    fresh.write_text("{}")
    spawn_ts = fresh.stat().st_mtime - 100  # spawn started BEFORE the file's mtime

    monkeypatch.setattr(wake.time, "sleep", lambda s: None)
    result = wake._wait_new_transcript(cfg, None, spawn_ts)
    assert result == str(fresh)


def test_spawn_wake_timeout_records_none_not_stale(cfg, monkeypatch):
    """If the NEW jsonl never appears within the poll window, record None (never
    the stale pre-spawn path). _window_rotated then treats the None hint on an
    alive, flag-free window as NOT rotated — the fallback must not reopen the
    loop."""
    from datetime import datetime as _dt

    from cortex import transcript, wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "p0-timeout"))
    conn.commit()

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True)
    (tdir / "OLD.jsonl").write_text("{}")  # only the stale file exists, no new one

    monkeypatch.setattr(window, "respawn", lambda c, initial_prompt=None, resume_sid=None: "sid-x")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    # Force an immediate timeout so the test does not sleep.
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: None)

    wake._spawn_wake(conn, cfg, _dt.now(timezone.utc))
    conn.close()
    assert wake_state.load(cfg)["transcript"] is None  # None, not the stale path

    wake_state.set_session_id(cfg, "sid-x")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)
    assert wake._window_rotated(cfg) is False  # None hint + alive -> not rotated


# --- rotate = flag for respawn, no /clear typing ------------------------------

def test_lie_down_explicit_rotate_sets_flag_no_typing(cfg, monkeypatch):
    """rotate=True flags a respawn for the next wake (session's explicit call)
    and does NOT type /clear (type_clear is gone). rotated=True in the result."""
    from cortex import window

    # type_clear must not exist anymore
    assert not hasattr(window, "type_clear")

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "rot"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 120_000, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 500}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg, rotate=True)
    assert r["rotated"] is True
    assert wake_state.take_rotated(cfg) is True  # flag set for the next wake


def test_lie_down_no_auto_rotate_over_line(cfg):
    """A big window no longer auto-rotates on lie_down (rotate is explicit)."""
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "norot"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 200_000, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 500}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg)  # no rotate flag
    assert r["rotated"] is False
    assert wake_state.take_rotated(cfg) is False


def test_lie_down_publishes_net_not_total(cfg):
    """lie_down records total occupancy to ct_wake_log but publishes NET spend
    (cache_creation + output) for the next wake's Budget 'net' line."""
    from cortex import note
    from cortex.pacemaker import integration

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "net"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()

    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True)
    # total occupancy 91_500 (big cache_read); net spend = 1_000 + 500 = 1_500
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 0, "cache_read_input_tokens": 90_000,
                  "cache_creation_input_tokens": 1_000, "output_tokens": 500}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg, force_slept="timeout")
    assert r["tokens"] == 91_500  # ct_wake_log records total occupancy
    conn = db.connect(cfg)
    try:
        assert note._window_tokens(conn) == 1_500  # Budget 'net' = net spend
        row = conn.execute(
            "SELECT tokens, net_tokens FROM ct_wake_log WHERE id=?", (wid,)).fetchone()
        assert row["tokens"] == 91_500 and row["net_tokens"] == 1_500
    finally:
        conn.close()


def test_daily_budget_line_sums_net_not_total(cfg):
    """note._today_tokens (the 'today X/Y' note line) sums NET spend, the
    same figure the gate reads — display and gate must agree."""
    from cortex import note
    from datetime import datetime as _dt, timezone as _tz

    now = _dt.now(_tz.utc)
    conn = db.connect(cfg)
    try:
        conn.execute(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens, net_tokens) "
            "VALUES (?,1,0,?,?)",
            (db.utcnow_iso(), 90_000, 1_500))
        conn.commit()
        assert note._today_tokens(conn, now) == 1_500  # net, not the 90k total
    finally:
        conn.close()


# --- lie_down next_wake (item 3) ----------------------------------------------

def test_lie_down_returns_next_wake_hm(cfg):
    """lie_down returns next_wake as local HH:MM (the marrow MCP wrapper surfaces
    it). An explicit next_wake_min pins the next floor to now + N (clamped)."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "nw"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)

    r = lie_down.lie_down(cfg, next_wake_min=20)
    assert "next_wake" in r
    tz = ZoneInfo(cfg["core"]["timezone"])
    expected = (_dt.now(tz) + timedelta(minutes=20)).strftime("%H:%M")
    # allow a 1-min clock-tick skew
    assert r["next_wake"] in (
        expected,
        (_dt.now(tz) + timedelta(minutes=21)).strftime("%H:%M"))


# --- resume vs fresh (item 6) -------------------------------------------------

def test_claude_session_id_from_transcript_stem(cfg):
    """claude_session_id = the recorded transcript jsonl stem (the conversation
    UUID for --resume), NOT the iTerm session id. None when no hint recorded."""
    from cortex import window

    assert window.claude_session_id(cfg) is None
    wake_state.update(cfg, transcript="/x/projects/cwd/abc-123.jsonl")
    assert window.claude_session_id(cfg) == "abc-123"


def test_claude_session_id_falls_back_to_newest_transcript_when_hint_none(cfg):
    """The recorded hint is a best-effort ~8s poll after spawn; the claude TUI
    can take 30s+ to create its session jsonl in real timing, so the hint is
    routinely None. When that happens, claude_session_id must fall back to the
    NEWEST top-level session jsonl in the transcript dir — in the died-window
    scenario that IS the dead session's own archive."""
    from cortex import window

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "dead-session-uuid.jsonl").write_text("{}\n")

    assert wake_state.load(cfg).get("transcript") is None  # no recorded hint
    assert window.claude_session_id(cfg) == "dead-session-uuid"


def test_claude_session_id_none_when_no_hint_and_no_transcript(cfg):
    """No recorded hint and no transcript file at all -> None (existing fresh
    fallback), never a fabricated UUID."""
    from cortex import window

    assert window.claude_session_id(cfg) is None


def test_launch_command_resume_variant(cfg):
    """launch_command bakes `--resume <sid>` when resume_sid is given."""
    from cortex import window

    cmd = window.launch_command(cfg, "☀️", resume_sid="abc-123")
    assert "--resume 'abc-123'" in cmd
    assert cmd.rstrip().endswith("'☀️'")
    plain = window.launch_command(cfg, "☀️")
    assert "--resume" not in plain


def test_window_wake_dead_resumes_when_sid_present(cfg, monkeypatch):
    """Item 6: a simply-dead resident (no rotate flag) with a recorded session
    UUID -> resume (respawn resume_sid set), no catchup line in the note. The
    relaunch prompt is the SAME composed emoji+marker prompt as a fresh spawn
    so the resumed window also gets its wake identity + note."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "resume"))
    conn.commit()

    wake_state.update(cfg, transcript="/x/projects/cwd/live-uuid.jsonl")
    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        (calls.__setitem__("resume_sid", resume_sid),
                         calls.__setitem__("prompt", initial_prompt)))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    now = _dt.now(timezone.utc)
    res = wake._window_wake(conn, cfg, "N", now)
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] == "live-uuid"   # same conversation resumed
    assert calls["prompt"] == window.fresh_initial_prompt(cfg, now)
    assert window.wake_prompt(cfg) in calls["prompt"]
    assert cfg["wake"].get("wake_signal_marker", "[CORTEX-WAKE]") in calls["prompt"]
    note_text = wake_state.wakeup_note_path(cfg).read_text()
    assert "died without a handoff" not in note_text  # resume -> no catchup


def test_window_wake_dead_resumes_from_newest_jsonl_when_hint_none(cfg, monkeypatch):
    """Real-timing regression: the recorded hint is None (the 8s spawn poll
    timed out before the 30s+ transcript creation), but a session jsonl exists
    in the transcript dir (the dead session's own archive) -> claude_session_id
    must still resolve it, and _window_wake must resume (not fresh-spawn)."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "resume"))
    conn.commit()

    assert wake_state.load(cfg).get("transcript") is None  # no recorded hint
    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "dead-session-uuid.jsonl").write_text("{}\n")

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        (calls.__setitem__("resume_sid", resume_sid),
                         calls.__setitem__("launch_command",
                                           window.launch_command(c, initial_prompt, resume_sid))))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] == "dead-session-uuid"
    assert "--resume 'dead-session-uuid'" in calls["launch_command"]


def test_window_wake_dead_no_sid_fresh_with_catchup(cfg, monkeypatch):
    """Item 6 fallback: a dead resident with NO recorded UUID -> fresh spawn
    (resume_sid None) AND the died-no-handoff catchup line in the note."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "fresh"))
    conn.commit()

    calls = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead, no transcript
    monkeypatch.setattr(wake, "_handoff_written_this_window", lambda c: False)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        calls.__setitem__("resume_sid", resume_sid))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["resume_sid"] is None          # no UUID -> fresh spawn
    note_text = wake_state.wakeup_note_path(cfg).read_text()
    assert "died without a handoff" in note_text  # fresh fallback -> catchup


def test_window_wake_plan_rotate_flag_is_fresh(cfg, monkeypatch):
    """_window_wake_plan: rotate flag -> 'fresh' (deliberate new brain), and the
    flag is consumed."""
    from cortex import wake, window

    wake_state.set_rotated(cfg)
    assert wake._window_wake_plan(cfg) == "fresh"
    assert wake_state.take_rotated(cfg) is False  # consumed by the plan call


def test_window_wake_plan_dead_no_flag_is_resume(cfg, monkeypatch):
    """_window_wake_plan: dead window with no rotate flag -> 'resume'."""
    from cortex import wake, window

    wake_state.set_session_id(cfg, "sid-dead")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: False)  # session gone
    assert wake._window_wake_plan(cfg) == "resume"
