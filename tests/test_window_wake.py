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

def test_window_wake_dispatch(cfg, monkeypatch):
    """Live window: _window_wake writes the note file, appends ONE WAKE signal
    line (no typing, no respawn), captures the wake row id, sets the awake
    marker, and lights the watchdog — verified without osascript."""
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
                        lambda c: calls.setdefault("respawn", True))
    monkeypatch.setattr(
        window, "append_wake_signal",
        lambda c, reason, note=None: calls.setdefault("signal", (reason, note)))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: True)
    monkeypatch.setattr(watchdog, "spawn", lambda c: calls.setdefault("watchdog", True))

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "NOTE-BODY", _dt.now(timezone.utc))
    conn.close()
    assert res == {"mode": "window", "session_id": None, "text": None}
    assert "respawn" not in calls               # live window is not respawned
    assert calls["signal"][0] == "floor"        # one WAKE line appended
    assert calls["watchdog"] is True
    # note file written with the note body
    assert wake_state.wakeup_note_path(cfg).read_text() == "NOTE-BODY"
    d = wake_state.load(cfg)
    assert d["awake"] is True and d["wake_log_id"] == wid


def test_window_wake_respawn_on_flag(cfg, monkeypatch):
    """respawn=True (rotate/rebirth) replaces the window before the signal."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "respawn"))
    conn.commit()

    calls = {}
    monkeypatch.setattr(window, "respawn", lambda c: calls.setdefault("respawn", True))
    monkeypatch.setattr(window, "append_wake_signal",
                        lambda c, reason, note=None: calls.setdefault("signal", reason))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: True)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc), respawn=True)
    conn.close()
    assert res["mode"] == "window"
    assert calls["respawn"] is True and calls["signal"] == "floor"


def test_window_wake_recovery_reappends_on_ear_miss(cfg, monkeypatch):
    """Signal did not land within ear_timeout -> respawn once + re-append."""
    from cortex import wake, watchdog, window

    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "recovery"))
    conn.commit()

    calls = {"respawn": 0, "signal": 0}
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(window, "respawn",
                        lambda c: calls.__setitem__("respawn", calls["respawn"] + 1))
    monkeypatch.setattr(window, "append_wake_signal",
                        lambda c, reason, note=None: calls.__setitem__("signal", calls["signal"] + 1))
    monkeypatch.setattr(wake, "_signal_landed", lambda c, before, t: False)  # never lands
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    from datetime import datetime as _dt
    res = wake._window_wake(conn, cfg, "N", _dt.now(timezone.utc))
    conn.close()
    assert res["mode"] == "window"
    assert calls["respawn"] == 1   # respawned exactly once on the miss
    assert calls["signal"] == 2    # original + one re-append


def test_window_wake_falls_back_on_window_error(cfg, monkeypatch):
    """An osascript/iTerm failure (WindowError) in the respawn/signal path ->
    None so the caller drops to the headless fallback; awake marker stays off."""
    from cortex import wake, window

    def boom(c):
        raise window.WindowError("no iterm")
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    monkeypatch.setattr(window, "append_wake_signal",
                        lambda c, reason, note=None: (_ for _ in ()).throw(
                            window.WindowError("append")))
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
    """store_window_tokens publishes to ct_pacemaker_state; bulletin reads it back
    (Budget line 'net Xk'). Survives lie_down's own floor-redraw save_state."""
    from cortex import bulletin
    from cortex.pacemaker import integration

    conn = db.connect(cfg)
    try:
        integration.store_window_tokens(conn, 88_000)
        assert bulletin._window_tokens(conn) == 88_000
        # a later floor-redraw save must NOT wipe it out of order
        integration.lie_down(conn, cfg)
        integration.store_window_tokens(conn, 90_000)
        assert bulletin._window_tokens(conn) == 90_000
    finally:
        conn.close()


# --- signal-file ear ----------------------------------------------------------

def test_append_wake_signal_line_format(cfg):
    """append_wake_signal writes one parseable line: WAKE for a normal wake,
    with reason + note + a UTC iso ts."""
    from cortex import window

    window.append_wake_signal(cfg, "floor", "/tmp/note.md")
    text = config.wake_signal_log_path(cfg).read_text().strip()
    assert text.startswith("WAKE ")
    assert "reason=floor" in text
    assert "note=/tmp/note.md" in text
    assert "ts=" in text and "+00:00" in text  # UTC-aware iso


def test_append_wake_signal_nudge_kind(cfg):
    """A 'nudge ...' reason (watchdog wrap-up) is a NUDGE line, not WAKE."""
    from cortex import window

    window.append_wake_signal(cfg, "nudge 写碎碎念收尾躺下")
    lines = config.wake_signal_log_path(cfg).read_text().strip().splitlines()
    assert lines[-1].startswith("NUDGE ")
    assert "reason=nudge 写碎碎念收尾躺下" in lines[-1]


def test_append_wake_signal_appends_not_overwrites(cfg):
    """Multiple signals accumulate (the ear tails the file)."""
    from cortex import window

    window.append_wake_signal(cfg, "floor", "/a")
    window.append_wake_signal(cfg, "floor", "/b")
    lines = config.wake_signal_log_path(cfg).read_text().strip().splitlines()
    assert len(lines) == 2


# --- arm prompt substituted into the launch command ---------------------------

def test_launch_command_embeds_arm_prompt_with_signal_log(cfg, tmp_path):
    """launch_command appends the arm prompt as claude's initial positional
    prompt, with {signal_log} substituted for the live signal-log path."""
    from cortex import window

    arm = tmp_path / "arm.md"
    arm.write_text("arm ear on {signal_log} then lie down")
    cfg["wake"]["arm_prompt_path"] = str(arm)
    sig = str(config.wake_signal_log_path(cfg))

    cmd = window.launch_command(cfg)
    assert f"arm ear on {sig} then lie down" in cmd
    assert "{signal_log}" not in cmd  # placeholder fully resolved


def test_launch_command_no_arm_prompt_when_missing(cfg):
    """No arm file -> no trailing prompt arg, window still launches."""
    from cortex import window

    cfg["wake"]["arm_prompt_path"] = "/nonexistent/arm.md"
    cmd = window.launch_command(cfg)
    assert cmd.rstrip().endswith("--dangerously-skip-permissions")


# --- rotate = flag for respawn, no /clear typing ------------------------------

def test_lie_down_over_rotate_sets_flag_no_typing(cfg, monkeypatch):
    """Over the rotate line, lie_down flags a rotate (next wake respawns) and
    does NOT type /clear (type_clear is gone). rotated=True in the result."""
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
    # occupancy over the default 100k rotate line
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 120_000, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 500}}}))
    wake_state.set_awake(cfg, wid, str(d / "s.jsonl"))

    r = lie_down.lie_down(cfg)
    assert r["rotated"] is True
    assert wake_state.take_rotated(cfg) is True  # flag set for the next wake


def test_lie_down_publishes_net_not_total(cfg):
    """lie_down records total occupancy to ct_wake_log but publishes NET spend
    (cache_creation + output) for the next wake's Budget 'net' line."""
    from cortex import bulletin
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
        assert bulletin._window_tokens(conn) == 1_500  # Budget 'net' = net spend
    finally:
        conn.close()
