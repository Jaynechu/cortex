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
    WAKE signal line (no respawn, no note-as-prompt), captures the wake row id,
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
                        lambda c, initial_prompt=None: calls.setdefault("respawn", True))
    monkeypatch.setattr(
        window, "append_wake_signal",
        lambda c, note: calls.setdefault("signal", note))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: True)
    monkeypatch.setattr(watchdog, "spawn", lambda c: calls.setdefault("watchdog", True))

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "NOTE-BODY", _dt.now(timezone.utc))
    conn.close()
    assert res == {"mode": "window", "session_id": None, "text": None}
    assert "respawn" not in calls               # live window is not respawned
    assert calls["signal"] == str(wake_state.wakeup_note_path(cfg))  # WAKE line = note path
    assert calls["watchdog"] is True
    # note file written with the note body
    assert wake_state.wakeup_note_path(cfg).read_text() == "NOTE-BODY"
    d = wake_state.load(cfg)
    assert d["awake"] is True and d["wake_log_id"] == wid


def test_window_wake_respawn_delivers_note_as_prompt(cfg, monkeypatch):
    """respawn=True (rotate/rebirth) spawns a FRESH window with the emoji-only
    first prompt baked in — no signal append, no notification (silent wake) —
    and sets the awake marker + watchdog."""
    from cortex import transcript, wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "respawn"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]

    calls = {}
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None: calls.setdefault("prompt", initial_prompt))
    assert not hasattr(window, "spawn_greeting")  # greeting mechanism removed
    monkeypatch.setattr(window, "append_wake_signal",
                        lambda c, note: calls.setdefault("signal", note))
    # New session jsonl appears promptly (skip the real 8s poll).
    monkeypatch.setattr(transcript, "newest",
                        lambda c: __import__("pathlib").Path("/t/new.jsonl"))
    monkeypatch.setattr(watchdog, "spawn", lambda c: calls.setdefault("watchdog", True))

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc), respawn=True)
    conn.close()
    assert res["mode"] == "window"
    note_path = str(wake_state.wakeup_note_path(cfg))
    assert calls["prompt"] == window.note_read_line(cfg, note_path)  # emoji baked in
    assert "signal" not in calls                # fresh path never appends a signal
    assert calls["watchdog"] is True
    d = wake_state.load(cfg)
    assert d["awake"] is True and d["wake_log_id"] == wid


def test_window_wake_recovery_respawns_with_note_on_ear_miss(cfg, monkeypatch):
    """Signal did not land within ear_timeout on an alive window -> spawn a
    fresh window that gets the note directly (no re-append)."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "recovery"))
    conn.commit()

    calls = {"respawn": 0, "signal": 0}
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None: calls.__setitem__("respawn", calls["respawn"] + 1))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(window, "append_wake_signal",
                        lambda c, note: calls.__setitem__("signal", calls["signal"] + 1))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: False)  # never lands
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["respawn"] == 1   # fresh window spawned exactly once on the miss
    assert calls["signal"] == 1    # only the original ear signal, no re-append


def test_window_wake_falls_back_on_window_error(cfg, monkeypatch):
    """An osascript/iTerm failure (WindowError) in the respawn path -> None so
    the caller drops to the headless fallback; awake marker stays off."""
    from cortex import wake, window

    def boom(c, initial_prompt=None):
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
    """append_wake_signal writes exactly one line: 'Waking up — read <note>
    first'. No reason field — the wake reason already lives inside the note
    itself (note's Wake: line), so it is not duplicated on the signal line."""
    from cortex import window

    window.append_wake_signal(cfg, "/tmp/note.md")
    text = config.wake_signal_log_path(cfg).read_text().strip()
    assert text == "Waking up — read /tmp/note.md first"


def test_append_wake_signal_appends_not_overwrites(cfg):
    """Multiple signals accumulate (the ear tails the file)."""
    from cortex import window

    window.append_wake_signal(cfg, "/a")
    window.append_wake_signal(cfg, "/b")
    lines = config.wake_signal_log_path(cfg).read_text().strip().splitlines()
    assert len(lines) == 2


# --- wakeup note baked into the launch command --------------------------------

def test_note_read_line_uses_config_template(cfg):
    """note_read_line renders the config wake_prompt with {note} substituted."""
    from cortex import window

    cfg["wake"]["wake_prompt"] = "GO: read {note} now"
    assert window.note_read_line(cfg, "/x/note.md") == "GO: read /x/note.md now"


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
    monkeypatch.setattr(window, "respawn", lambda c, initial_prompt=None: "sid-new")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    # Zero the poll sleep so the test does not actually wait.
    monkeypatch.setattr(wake.time, "sleep", lambda s: None)

    wake._spawn_wake(conn, cfg, str(wake_state.wakeup_note_path(cfg)),
                     _dt.now(timezone.utc))
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

    monkeypatch.setattr(window, "respawn", lambda c, initial_prompt=None: "sid-x")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    # Force an immediate timeout so the test does not sleep.
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: None)

    wake._spawn_wake(conn, cfg, "x", _dt.now(timezone.utc))
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
