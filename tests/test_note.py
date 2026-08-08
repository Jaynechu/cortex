from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cortex import config, note

MEL = ZoneInfo("Australia/Melbourne")
NOW = datetime(2026, 7, 8, 14, 30, tzinfo=MEL)


@pytest.fixture
def cfg(tmp_path):
    # Pure defaults: point load at a nonexistent path so no live cortex.toml leaks in.
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["cortex_home"] = str(tmp_path / "home")
    # Keeps every marrow-side resolution (breaker.json, shell ledgers) under
    # tmp_path — a default marrow_db would point them at the live dir.
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    # These tests assert the note BODY structure (Now: / title). The Fix 5
    # machine-origin tag is a separate first line, verified in test_wake_regime_fixes;
    # blank it here so the body assertions (startswith "Now:" / title) stay exact.
    c["note"]["wake_machine_tag"] = ""
    return c


@pytest.fixture(autouse=True)
def no_real_macos_probes(monkeypatch):
    """Every macOS probe is explicitly mocked by the test that exercises it."""
    def fail(*_args, **_kwargs):
        raise OSError("real macOS probe disabled in tests")

    monkeypatch.setattr(note.ctypes, "CDLL", fail)
    monkeypatch.setattr(note.subprocess, "run", fail)


def make_events_table(conn):
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "timestamp TEXT, role TEXT, content TEXT, channel TEXT)"
    )
    conn.commit()


def _row_id(conn, content: str) -> int:
    """events.id of the row carrying `content` (row-id cursor tests)."""
    return conn.execute("SELECT id FROM events WHERE content=?",
                        (content,)).fetchone()[0]


# --------------------------------------------------------------------------- #
# render — full note + omissions
# --------------------------------------------------------------------------- #

class _FloorReason:
    """Stand-in for a retired trigger-reason object (the Wake line is gone; the
    renderer must still ignore whatever `reasons` carries)."""
    kind = "floor"
    detail = "floor check due"


def test_render_full_note(cfg):
    data = {
        "wake_parts": ["wander"],
        "last_wake": {"minutes_ago": 12, "force_slept": None},
        "last_active": {"minutes_ago": 12},
        "computer": {
            "state": "active", "app": "Google Chrome", "idle_seconds": 60,
        },
        "pending": [{"hm": "00:18", "intent": "去看看猫睡了没"}],
    }
    text = note.render(cfg, NOW, data)
    assert "Wake:" not in text  # reason line retired
    assert "Now:" not in text  # Now line deleted — hook injects current time
    assert text.startswith("🐆 Cortex last wake: 12m ago | 💻 Mac is Active: Google Chrome")
    assert "Plan Used" not in text  # budget line retired
    # header = exactly the merged line above, nothing else
    assert text.split("\n\n---\n\n")[0].split("\n") == [
        "🐆 Cortex last wake: 12m ago | 💻 Mac is Active: Google Chrome",
    ]
    assert "Pending self-schedule: due 00:18 去看看猫睡了没" in text
    # block separators
    assert "\n\n---\n\n" in text
    # cal/rem retired
    assert "Cal:" not in text and "Rem:" not in text


def test_render_omits_absent_lines(cfg):
    text = note.render(cfg, NOW, {})
    assert "Wake:" not in text  # reason line retired
    assert "Now:" not in text  # Now line deleted
    assert text == ""  # nothing to report -> empty header, no lines at all
    assert "Cortex last wake:" not in text
    assert "Plan Used:" not in text
    assert "Active (Mac):" not in text
    assert "Pending" not in text
    assert "### Replay" not in text


def test_render_kick_reasons_plain_lines_no_header(cfg):
    data = {"kick_reasons": ['Msg #3 replied: "miss you"', "She's up — day mode"]}
    text = note.render(cfg, NOW, data)
    assert 'Msg #3 replied: "miss you"' in text
    assert "She's up — day mode" in text
    assert "### Woke for" not in text          # header rejected
    assert "Woke for" not in text


def test_render_kick_reasons_absent_when_empty(cfg):
    assert "Woke for" not in note.render(cfg, NOW, {"kick_reasons": []})


def _home_cfg(cfg, tmp_path):
    """cfg with tmp_path state paths so wake_state writes stay off the live dir
    (conftest hard-wall)."""
    home = tmp_path / "cortex"
    (home / "state").mkdir(parents=True, exist_ok=True)
    cfg = dict(cfg)
    cfg["paths"] = {
        **cfg.get("paths", {}),
        "cortex_home": str(home),
        "wake_state_file": str(home / "state" / "wake_state.json"),
        "watchdog_pidfile": str(home / "state" / "watchdog.pid"),
    }
    return cfg


def test_gather_kick_reasons_settle_on_injection(cfg, tmp_path):
    import sqlite3

    from cortex import wake_state
    cfg = _home_cfg(cfg, tmp_path)
    wake_state.update(cfg, kick_reasons=['Msg #1 replied: "hi"'])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    # consume_kick=True + settle=True (real injection) renders + clears the flags.
    data = note.gather(conn, cfg, NOW, consume_kick=True, settle=True)
    assert data["kick_reasons"] == ['Msg #1 replied: "hi"']
    assert wake_state.load(cfg).get("kick_reasons") in (None, [])
    # A second settled note sees nothing (already consumed).
    data2 = note.gather(conn, cfg, NOW, consume_kick=True, settle=True)
    assert data2["kick_reasons"] == []


def test_gather_kick_reasons_peek_without_settle_keeps_pending(cfg, tmp_path):
    import sqlite3

    from cortex import wake_state
    cfg = _home_cfg(cfg, tmp_path)
    wake_state.update(cfg, kick_reasons=['Msg #1 replied: "hi"'])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    # consume_kick=True, settle=False (frozen note that may be discarded):
    # renders the reason but does NOT clear it — pending-visible.
    data = note.gather(conn, cfg, NOW, consume_kick=True)
    assert data["kick_reasons"] == ['Msg #1 replied: "hi"']
    assert wake_state.load(cfg).get("kick_reasons") == ['Msg #1 replied: "hi"']


def test_gather_passive_render_peeks_kick_reasons(cfg, tmp_path):
    import sqlite3

    from cortex import wake_state
    cfg = _home_cfg(cfg, tmp_path)
    wake_state.update(cfg, kick_reasons=['Msg #2 no reply in 30min'])
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    # consume_kick=False (render-only path) PEEKS the reason so an un-injected
    # kick surfaces in the fresh render — must NOT clear it.
    data = note.gather(conn, cfg, NOW)
    assert data["kick_reasons"] == ['Msg #2 no reply in 30min']
    assert wake_state.load(cfg).get("kick_reasons") == ['Msg #2 no reply in 30min']


def test_render_turn_end_line_omitted_by_default(cfg):
    # Default turn_end_text is "" (T2: the silence cycle carries this info now).
    text = note.render(cfg, NOW, {})
    assert "NOTE:" not in text


def test_render_turn_end_line_appears_with_custom_template(cfg):
    # Clamp numbers still render from config when a custom template is set.
    # T3: the merged clamp floor is 0 for every hour (0 = immediate re-wake).
    cfg["note"]["turn_end_text"] = (
        "NOTE: lie_down(next_wake_min=N) [{next_wake_min}-{next_wake_max}].")
    text = note.render(cfg, NOW, {})
    assert text.rstrip().endswith(
        "NOTE: lie_down(next_wake_min=N) [0-360].")


def test_render_title_prepended_single_newline(cfg):
    cfg["note"]["title"] = "📮 小道消息"
    text = note.render(cfg, NOW, {})
    assert text == "📮 小道消息\n"


def test_render_title_empty_omits_it(cfg):
    text = note.render(cfg, NOW, {})
    assert text == ""
    assert "小道消息" not in text


def test_render_pause_tag_from_breaker(cfg):
    """The pause tag is the breaker's, and it names the reason."""
    data = {"last_wake": {"minutes_ago": 40, "force_slept": None},
            "paused": {"reason": "manual", "scope": "cli"}}
    text = note.render(cfg, NOW, data)
    assert "Cortex last wake: 40m ago (paused: manual)" in text


def test_render_no_pause_tag_when_breaker_clear(cfg):
    """No breaker -> no tag, whatever ct_wake_log.force_slept says: that column
    is the cli window's own shutdown ledger, not a per-shell pause state."""
    data = {"last_wake": {"minutes_ago": 40, "force_slept": "timeout"}}
    text = note.render(cfg, NOW, data)
    assert "Cortex last wake: 40m ago" in text
    assert "force-slept" not in text and "paused" not in text


def test_render_pause_tag_empty_config_omits_it(cfg):
    cfg["note"]["pause_tag"] = ""
    data = {"last_wake": {"minutes_ago": 40}, "paused": {"reason": "auto_fuse"}}
    assert "paused" not in note.render(cfg, NOW, data)


def test_render_pause_tag_without_activity_line(cfg):
    """Nothing to report on activity -> the tag still shows: the pause is a fact
    about the shell, not about the last reply."""
    text = note.render(cfg, NOW, {"paused": {"reason": "auto_fuse"}})
    assert text.startswith("🐆 ") and "(paused: auto_fuse)" in text


def test_render_no_wake_line_ever(cfg):
    """The 'Wake:' reason line is fully retired — gone from every render."""
    for data in ({}, {"wake_parts": ["wander"]}, {"last_wake": {"minutes_ago": 5, "force_slept": None}}):
        assert "Wake:" not in note.render(cfg, NOW, data)


# --------------------------------------------------------------------------- #
# DB-sourced facts
# --------------------------------------------------------------------------- #

def test_last_wake_skips_current_and_marks_force_slept(marrow_conn):
    prev = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    cur = (NOW - timedelta(seconds=10)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept) VALUES (?, 1, 0, ?)",
        [(prev, "timeout"), (cur, None)],
    )
    marrow_conn.commit()
    lw = note._last_wake(marrow_conn, NOW)
    assert lw["minutes_ago"] == 30
    assert lw["force_slept"] == "timeout"
    assert lw["ts"] == prev


def test_last_wake_none_when_only_current(marrow_conn):
    cur = (NOW - timedelta(seconds=5)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)", (cur,))
    marrow_conn.commit()
    assert note._last_wake(marrow_conn, NOW) is None


def _stamp_activity(conn, rows):
    """rows: (minutes_ago, sid, channel)."""
    conn.executemany(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        [((NOW - timedelta(minutes=m)).astimezone(ZoneInfo("UTC")).isoformat(),
          sid, ch) for m, sid, ch in rows])
    conn.commit()


def _register_sids(cfg, cli_sid=None, tg_sid=None):
    """Write the two shell-owned sid records the render reads."""
    from cortex import wake_state
    if cli_sid:
        wake_state.update(cfg, cortex_claude_sid=cli_sid)
    if tg_sid:
        d = config.shell_state_dir(cfg)
        d.mkdir(parents=True, exist_ok=True)
        (d / "tg.json").write_text(json.dumps({"session_id": tg_sid}),
                                   encoding="utf-8")


def test_last_active_newest_row_for_this_shells_sid(marrow_conn, cfg):
    """Minutes come from the newest row of THIS shell's own claude sid. The
    channel column is unusable: channel='cli' tags every cli Claude Code window
    on the machine, so a stranger session must not read as cortex activity."""
    _register_sids(cfg, cli_sid="cli-sid", tg_sid="tg-sid")
    _stamp_activity(marrow_conn, [
        (1, "some-other-window", "cli"),   # user's own cli session — ignored
        (3, "tg-sid", "tg"),
        (7, "cli-sid", "cli"),
    ])
    la = note._last_active(marrow_conn, cfg, NOW, "cli")
    assert la["minutes_ago"] == 7
    la_tg = note._last_active(marrow_conn, cfg, NOW, "tg")
    assert la_tg["minutes_ago"] == 3


def test_last_active_defaults_to_cli_shell(marrow_conn, cfg):
    """No shell argument -> the cli shell (the unqualified render)."""
    _register_sids(cfg, cli_sid="cli-sid")
    _stamp_activity(marrow_conn, [(3, "cli-sid", "cli")])
    assert note._last_active(marrow_conn, cfg, NOW)["minutes_ago"] == 3


def test_last_active_none_without_row_for_this_sid(marrow_conn, cfg):
    """This shell's sid has no row -> None (render falls back to the wake row);
    another session's activity must never fill the gap."""
    _register_sids(cfg, cli_sid="cli-sid", tg_sid="tg-sid")
    _stamp_activity(marrow_conn, [(2, "cli-sid", "cli")])
    assert note._last_active(marrow_conn, cfg, NOW, "tg") is None


def test_last_active_none_when_sid_unresolvable(marrow_conn, cfg):
    """No recorded sid for the shell -> None, never a guess off the channel."""
    _stamp_activity(marrow_conn, [(2, "whoever", "cli")])
    assert note._last_active(marrow_conn, cfg, NOW, "cli") is None
    assert note._last_active(marrow_conn, cfg, NOW, "tg") is None


def test_paused_is_per_shell(cfg, tmp_path):
    """scope=cli tags only the cli note, scope=tg only the tg note, scope=all
    both — the breaker is the note's single pause source."""
    from cortex import breaker
    d = config.marrow_config_dir(cfg)
    breaker.trip(d, "cli", "manual")
    assert note._paused(cfg, "cli") == {"reason": "manual", "scope": "cli"}
    assert note._paused(cfg, "tg") is None
    breaker.trip(d, "tg", "auto_fuse")
    assert note._paused(cfg, "cli") is None
    assert note._paused(cfg, "tg") == {"reason": "auto_fuse", "scope": "tg"}
    breaker.trip(d, "all", "manual")
    assert note._paused(cfg, "cli") and note._paused(cfg, "tg")
    breaker.clear(d)
    assert note._paused(cfg, "cli") is None and note._paused(cfg, "tg") is None


def test_render_last_active_falls_back_to_wake_minutes(cfg):
    """No last_active -> the line uses the wake row's minutes and keeps the
    'Cortex last wake:' label."""
    data = {"last_wake": {"minutes_ago": 22, "force_slept": "timeout"}}
    text = note.render(cfg, NOW, data)
    assert "Cortex last wake: 22m ago" in text


def test_render_last_active_overrides_wake_minutes(cfg):
    """last_active minutes win over the wake row's minutes."""
    data = {
        "last_wake": {"minutes_ago": 40, "force_slept": "timeout"},
        "last_active": {"minutes_ago": 3},
    }
    text = note.render(cfg, NOW, data)
    assert "Cortex last wake: 3m ago" in text


def test_pending_within_window(cfg, tmp_path, monkeypatch):
    sp = tmp_path / "ss.json"
    due_soon = (NOW + timedelta(minutes=10)).isoformat()
    due_far = (NOW + timedelta(minutes=40)).isoformat()
    sp.write_text(json.dumps([
        {"due_at": due_soon, "intent": "喝水"},
        {"due_at": due_far, "intent": "太远"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)
    pend = note._pending(cfg, NOW)
    assert pend == [{"hm": (NOW + timedelta(minutes=10)).strftime("%H:%M"), "intent": "喝水"}]


def test_pending_missing_file(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "self_schedule_path", lambda c: tmp_path / "nope.json")
    assert note._pending(cfg, NOW) == []


def test_pending_naive_aware_and_garbage_mixed(cfg, tmp_path, monkeypatch):
    """Regression: a naive (offset-free local) due_at used to raise TypeError
    comparing naive vs aware datetimes, crashing the whole note. Naive + aware
    + garbage entries in one file -> the two valid ones render, garbage
    skipped, no exception."""
    sp = tmp_path / "ss.json"
    naive_due = (NOW + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    aware_due = (NOW + timedelta(minutes=10)).isoformat()
    sp.write_text(json.dumps([
        {"due_at": naive_due, "intent": "naive"},
        {"due_at": aware_due, "intent": "aware"},
        {"due_at": "not-a-date", "intent": "garbage"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)
    pend = note._pending(cfg, NOW)
    assert pend == [
        {"hm": (NOW + timedelta(minutes=5)).strftime("%H:%M"), "intent": "naive"},
        {"hm": (NOW + timedelta(minutes=10)).strftime("%H:%M"), "intent": "aware"},
    ]


def test_frontmost_app_loginwindow_returns_none(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "loginwindow\n"
    calls = []
    monkeypatch.setattr(
        note.subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or FakeProc())
    assert note._frontmost_app() is None
    assert calls[0][0][0][0] == "/usr/bin/osascript"


def test_frontmost_app_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no osascript")
    monkeypatch.setattr(note.subprocess, "run", boom)
    assert note._frontmost_app() is None


def test_frontmost_app_ok(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "WeChat\n"
    calls = []
    monkeypatch.setattr(
        note.subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or FakeProc())
    assert note._frontmost_app() == "WeChat"
    assert calls[0][0][0][0] == "/usr/bin/osascript"


class _CFunc:
    def __init__(self, fn):
        self.fn = fn

    def __call__(self, *args):
        return self.fn(*args)


@pytest.mark.parametrize(("on_console", "locked", "expected"), [
    (True, False, False),
    (True, None, False),
    (True, True, True),
    (False, False, True),
    (None, False, None),
])
def test_screen_locked_reads_coregraphics(
        monkeypatch, on_console, locked, expected):
    releases = []
    cg = type("CG", (), {})()
    cf = type("CF", (), {})()
    cg.CGSessionCopyCurrentDictionary = _CFunc(lambda: 1)
    cf.CFStringCreateWithCString = _CFunc(
        lambda _allocator, name, _encoding:
            {b"kCGSSessionOnConsoleKey": 2,
             b"CGSSessionScreenIsLocked": 3}[name])
    cf.CFDictionaryGetValue = _CFunc(
        lambda _session, key:
            None if ((key == 2 and on_console is None)
                     or (key == 3 and locked is None))
            else {2: 4, 3: 5}[key])
    cf.CFBooleanGetValue = _CFunc(
        lambda value: {4: on_console, 5: locked}[value])
    cf.CFRelease = _CFunc(releases.append)
    monkeypatch.setattr(
        note.ctypes, "CDLL",
        lambda path: cg if path == note._APPLICATION_SERVICES else cf)

    assert note._screen_locked() is expected
    expected_releases = [2, 1] if on_console is None else [2, 3, 1]
    assert releases == expected_releases


def test_screen_locked_ctypes_failure_returns_none(monkeypatch):
    def fail(_path):
        raise OSError("CoreGraphics unavailable")

    monkeypatch.setattr(note.ctypes, "CDLL", fail)
    assert note._screen_locked() is None


@pytest.mark.parametrize(("stdout", "returncode", "expected"), [
    ('    | |   "HIDIdleTime" = 2400000000000\n', 0, 2400),
    ('    | |   "Other" = 1\n', 0, None),
    ('', 1, None),
])
def test_idle_seconds_parses_ioreg(monkeypatch, stdout, returncode, expected):
    proc = type("Proc", (), {"stdout": stdout, "returncode": returncode})()
    calls = []
    monkeypatch.setattr(
        note.subprocess, "run",
        lambda *a, **k: calls.append((a, k)) or proc)
    assert note._idle_seconds() == expected
    assert calls[0][0][0] == ["/usr/sbin/ioreg", "-c", "IOHIDSystem"]


def test_idle_seconds_subprocess_failure_returns_none(monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("ioreg unavailable")

    monkeypatch.setattr(note.subprocess, "run", fail)
    assert note._idle_seconds() is None


def test_computer_status_locked_does_not_report_app(cfg, monkeypatch):
    monkeypatch.setattr(note, "_screen_locked", lambda: True)
    monkeypatch.setattr(note, "_idle_seconds", lambda: 12 * 3600 + 59 * 60)

    def fail():
        raise AssertionError("locked state must not probe the app")

    monkeypatch.setattr(note, "_frontmost_app", fail)
    assert note._computer_status(cfg) == {
        "state": "locked", "idle_seconds": 12 * 3600 + 59 * 60,
    }


def test_computer_status_lock_failure_degrades_to_unlocked(cfg, monkeypatch):
    monkeypatch.setattr(note, "_screen_locked", lambda: None)
    monkeypatch.setattr(note, "_idle_seconds", lambda: 40 * 60)
    monkeypatch.setattr(note, "_frontmost_app", lambda: "Code")
    monkeypatch.setattr(note.config, "away_idle_min", lambda _cfg: 20)
    assert note._computer_status(cfg) == {
        "state": "away", "app": "Code", "idle_seconds": 40 * 60,
    }


@pytest.mark.parametrize(("idle_seconds", "state"), [
    (20 * 60 - 1, "active"),
    (20 * 60, "away"),
])
def test_computer_status_away_threshold(cfg, monkeypatch, idle_seconds, state):
    monkeypatch.setattr(note, "_screen_locked", lambda: False)
    monkeypatch.setattr(note, "_idle_seconds", lambda: idle_seconds)
    monkeypatch.setattr(note, "_frontmost_app", lambda: "Code")
    monkeypatch.setattr(note.config, "away_idle_min", lambda _cfg: 20)
    assert note._computer_status(cfg)["state"] == state


def test_computer_status_omitted_when_idle_fails(cfg, monkeypatch):
    monkeypatch.setattr(note, "_screen_locked", lambda: False)
    monkeypatch.setattr(note, "_idle_seconds", lambda: None)
    assert note._computer_status(cfg) is None


@pytest.mark.parametrize(("seconds", "expected"), [
    (40 * 60, "40m"),
    (59 * 60 + 59, "59m"),
    (60 * 60, "1h"),
    (60 * 60 + 20 * 60, "1h20m"),
    (12 * 60 * 60 + 59 * 60, "12h59m"),
])
def test_idle_duration(seconds, expected):
    assert note._idle_duration(seconds) == expected


@pytest.mark.parametrize(("computer", "expected"), [
    ({"state": "locked", "idle_seconds": 12 * 3600},
     "💻 Mac is Locked: 12h"),
    ({"state": "away", "app": "Code", "idle_seconds": 40 * 60},
     "💻 Mac is Inactive: 40m Code"),
    ({"state": "active", "app": "Code", "idle_seconds": 5 * 60},
     "💻 Mac is Active: Code"),
])
def test_render_computer_three_states(computer, expected):
    assert note._render_computer(computer) == expected


# --------------------------------------------------------------------------- #
# gather integration (external facts stubbed)
# --------------------------------------------------------------------------- #

def test_gather_end_to_end(marrow_conn, cfg, tmp_path, monkeypatch):
    # Isolate wake_state so a stale cursor on a real machine's live state
    # (diff-mode baseline, D6) can never filter out this fixture's row.
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "hi", "wx"))
    marrow_conn.commit()

    monkeypatch.setattr(note, "_computer_status", lambda _cfg: None)

    data = note.gather(marrow_conn, cfg, NOW, decision={
        "reasons": [_FloorReason()]})
    assert "wake_parts" not in data  # Wake reason line retired
    assert "budget" not in data  # budget line retired
    assert "replay" not in data  # Replay section deleted — turn_inject is the channel
    assert "handoff" not in data  # handoff moved to SessionStart
    text = note.render(cfg, NOW, data)
    assert "Now:" not in text  # Now line deleted
    assert "Wake:" not in text


# --------------------------------------------------------------------------- #
# gather diff mode (D6): last_note_row_id cursor persisted + advanced per render
# --------------------------------------------------------------------------- #

def _seed_prev_wake(conn, minutes_ago: int) -> str:
    """Write a prior wake=1 row `minutes_ago` before NOW; return its ISO ts."""
    ts = (NOW - timedelta(minutes=minutes_ago)).astimezone(ZoneInfo("UTC")).isoformat()
    conn.execute("INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)", (ts,))
    conn.commit()
    return ts


def test_last_wake_after_short_rotate_cycle_reports_minutes(marrow_conn):
    """BUG A (note side): with the previously-missing wake rows now written, a
    16-min rotate cycle's most recent wake row (outside the current-wake epsilon)
    is what 'Last wake' reports — ~16min, not the noon scheduled wake hours back."""
    # Noon scheduled wake (hours ago) + a rotate-cycle wake 16 min ago.
    noon = (NOW - timedelta(minutes=280)).astimezone(ZoneInfo("UTC")).isoformat()
    rotate = (NOW - timedelta(minutes=16)).astimezone(ZoneInfo("UTC")).isoformat()
    cur = (NOW - timedelta(seconds=5)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons) VALUES (?, 1, 0, ?)",
        [(noon, "floor"), (rotate, "rotate"), (cur, "user")])
    marrow_conn.commit()
    lw = note._last_wake(marrow_conn, NOW)
    assert lw["minutes_ago"] == 16  # the rotate cycle wake, not noon


def test_gather_survives_naive_due_at_self_schedule(marrow_conn, cfg, tmp_path, monkeypatch):
    """Live-repro regression: a self_schedule.json entry with an offset-free
    (naive) due_at must not crash gather()/render() end-to-end."""
    make_events_table(marrow_conn)
    marrow_conn.commit()

    monkeypatch.setattr(note, "_computer_status", lambda _cfg: None)

    sp = tmp_path / "ss.json"
    naive_due = (NOW + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    sp.write_text(json.dumps([{"due_at": naive_due, "intent": "x"}]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)

    data = note.gather(marrow_conn, cfg, NOW)
    assert data["pending"] == [
        {"hm": (NOW + timedelta(minutes=5)).strftime("%H:%M"), "intent": "x"}
    ]
    text = note.render(cfg, NOW, data)
    assert "Pending self-schedule" in text


# --------------------------------------------------------------------------- #
# location (📍) — ct-location-sensor T5, 7 locked shapes
# --------------------------------------------------------------------------- #

def _write_location(tmp_path, monkeypatch, data):
    p = tmp_path / "location.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(note, "_location_path", lambda c: p)
    return p


def test_location_missing_file_no_line(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(note, "_location_path", lambda c: tmp_path / "nope.json")
    assert note._location(cfg) is None
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍" not in text


def test_location_corrupt_file_no_line(cfg, tmp_path, monkeypatch):
    p = tmp_path / "location.json"
    p.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(note, "_location_path", lambda c: p)
    assert note._location(cfg) is None


def test_render_location_two_hop(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": "Deakin", "since": "2026-07-08T12:15:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Home", "ts": "2026-07-08T08:30:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    loc = note._location(cfg)
    text = note.render(cfg, NOW, {"location": loc})
    assert "📍 08:30 Left Home → 12:15 Arrived Deakin (2h15m)" in text


def test_render_location_no_prev(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": "Deakin", "since": "2026-07-08T12:15:00", "seeded": False,
        "prev": None, "last_seen": "2026-07-08T14:00:00",
    })
    loc = note._location(cfg)
    text = note.render(cfg, NOW, {"location": loc})
    assert "📍 12:15 Arrived Deakin (2h15m)" in text


def test_render_location_out_leave(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": None, "since": "2026-07-08T13:45:00", "seeded": False,
        "prev": {"event": "enter", "zone": "Home", "ts": "2026-07-08T09:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    loc = note._location(cfg)
    text = note.render(cfg, NOW, {"location": loc})
    assert "📍 13:45 Left Home (45m)" in text


def test_render_location_cold_start_seeded(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": "Deakin", "since": None, "seeded": True,
        "prev": None, "last_seen": "2026-07-08T14:00:00",
    })
    loc = note._location(cfg)
    text = note.render(cfg, NOW, {"location": loc})
    assert "📍 Deakin" in text
    assert "📍 Deakin (" not in text  # no clock, no duration ever invented


def test_render_location_cross_day_weekday_prefix(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": "Deakin", "since": "2026-07-08T07:00:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Home", "ts": "2026-07-07T22:30:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    loc = note._location(cfg)
    text = note.render(cfg, NOW, {"location": loc})
    assert "📍 Tue 22:30 Left Home → 07:00 Arrived Deakin (7h30m)" in text


def test_loc_duration_formatting(cfg):
    assert note._loc_duration(timedelta(minutes=39)) == "39m"
    assert note._loc_duration(timedelta(minutes=135)) == "2h15m"


def test_loc_duration_day_tier(cfg):
    assert note._loc_duration(timedelta(hours=23, minutes=59)) == "23h59m"
    assert note._loc_duration(timedelta(hours=24)) == "1d0h"
    assert note._loc_duration(timedelta(hours=50, minutes=35)) == "2d2h"
    assert note._loc_duration(timedelta(days=3, hours=2, minutes=40)) == "3d2h"


def test_render_location_under_threshold_keeps_hop_trail(cfg, tmp_path, monkeypatch):
    """20h in the zone is under location_hop_max_hours -> full hop trail."""
    _write_location(tmp_path, monkeypatch, {
        "zone": "Home", "since": "2026-07-07T18:30:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Deakin", "ts": "2026-07-07T18:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Tue 18:00 Left Deakin → Tue 18:30 Arrived Home (20h0m)" in text


def test_render_location_over_threshold_collapses(cfg, tmp_path, monkeypatch):
    """Past the threshold the arrival time AND the prior hop both go."""
    _write_location(tmp_path, monkeypatch, {
        "zone": "Home", "since": "2026-07-07T13:30:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Deakin", "ts": "2026-07-07T13:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Home (1d1h)" in text
    assert "Deakin" not in text
    assert "Arrived" not in text


def test_render_location_collapses_multi_day(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": "Home", "since": "2026-07-05T12:30:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Deakin", "ts": "2026-07-05T12:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Home (3d2h)" in text


def test_render_location_collapse_without_prev(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": "Home", "since": "2026-07-05T12:30:00", "seeded": False,
        "prev": None, "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Home (3d2h)" in text


def test_render_location_out_branch_never_collapses(cfg, tmp_path, monkeypatch):
    """Outside every known zone for days IS the signal — keep the full shape."""
    _write_location(tmp_path, monkeypatch, {
        "zone": None, "since": "2026-07-05T12:30:00", "seeded": False,
        "prev": {"event": "enter", "zone": "Home", "ts": "2026-07-05T09:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Sun 12:30 Left Home (3d2h)" in text


def test_render_location_hop_max_zero_disables_collapse(cfg, tmp_path, monkeypatch):
    cfg["note"]["location_hop_max_hours"] = 0
    _write_location(tmp_path, monkeypatch, {
        "zone": "Home", "since": "2026-07-05T12:30:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Deakin", "ts": "2026-07-05T12:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Sun 12:00 Left Deakin → Sun 12:30 Arrived Home (3d2h)" in text


def test_render_location_hop_max_custom_threshold(cfg, tmp_path, monkeypatch):
    cfg["note"]["location_hop_max_hours"] = 2
    _write_location(tmp_path, monkeypatch, {
        "zone": "Deakin", "since": "2026-07-08T12:15:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Home", "ts": "2026-07-08T08:30:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Deakin (2h15m)" in text


def test_render_location_hop_max_invalid_falls_back_to_24(cfg, tmp_path, monkeypatch):
    cfg["note"]["location_hop_max_hours"] = "later"
    _write_location(tmp_path, monkeypatch, {
        "zone": "Home", "since": "2026-07-05T12:30:00", "seeded": False,
        "prev": {"event": "leave", "zone": "Deakin", "ts": "2026-07-05T12:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍 Home (3d2h)" in text


def test_render_location_collapse_never_invents_on_bad_since(cfg, tmp_path, monkeypatch):
    _write_location(tmp_path, monkeypatch, {
        "zone": "Home", "since": "not-a-timestamp", "seeded": False,
        "prev": {"event": "leave", "zone": "Deakin", "ts": "2026-07-05T12:00:00"},
        "last_seen": "2026-07-08T14:00:00",
    })
    text = note.render(cfg, NOW, {"location": note._location(cfg)})
    assert "📍" not in text


def test_render_locked_header_shape_tag_then_location_then_merged_line(cfg):
    """The exact locked header (coordinator spec): machine tag, then 📍, then
    one merged "🐆 Cortex last wake ... | 💻 Mac is Active ..." line — no
    "Now:" line anywhere."""
    cfg["note"]["wake_machine_tag"] = (
        "[AUTOMATED WAKE SIGNAL — Note delivered by the scheduler]")
    data = {
        "location": {
            "zone": "Deakin", "since": "2026-07-08T09:00:00", "seeded": False,
            "prev": {"event": "leave", "zone": "Home", "ts": "2026-07-08T08:30:00"},
            "last_seen": "2026-07-08T14:00:00",
        },
        "last_active": {"minutes_ago": 19},
        "computer": {"state": "active", "app": "Notion", "idle_seconds": 60},
    }
    text = note.render(cfg, NOW, data)
    assert text == (
        "[AUTOMATED WAKE SIGNAL — Note delivered by the scheduler]\n"
        "📍 08:30 Left Home → 09:00 Arrived Deakin (5h30m)\n"
        "🐆 Cortex last wake: 19m ago | 💻 Mac is Active: Notion"
    )


def test_render_location_omitted_when_no_state_active_line_unaffected(cfg):
    """No location state -> merged active line is still the first (only)
    header line — unchanged position logic, just no 📍 line."""
    data = {
        "last_active": {"minutes_ago": 19},
        "computer": {"state": "active", "app": "Notion", "idle_seconds": 60},
    }
    text = note.render(cfg, NOW, data)
    assert text == "🐆 Cortex last wake: 19m ago | 💻 Mac is Active: Notion"
    assert "📍" not in text


def test_render_merged_line_active_only_no_pipe(cfg):
    """No Active(Mac) app -> just the 🐆 half, no trailing pipe."""
    data = {"last_active": {"minutes_ago": 19}}
    text = note.render(cfg, NOW, data)
    assert text == "🐆 Cortex last wake: 19m ago"
    assert "|" not in text


def test_gather_location_uses_location_path(cfg, tmp_path, monkeypatch):
    import sqlite3

    _write_location(tmp_path, monkeypatch, {
        "zone": "Deakin", "since": None, "seeded": True,
        "prev": None, "last_seen": "2026-07-08T14:00:00",
    })
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    make_events_table(conn)
    data = note.gather(conn, cfg, NOW)
    assert data["location"] == {
        "zone": "Deakin", "since": None, "seeded": True,
        "prev": None, "last_seen": "2026-07-08T14:00:00",
    }


# --------------------------------------------------------------------------- #
# Window-line SID override (caller transcript beats wake_state)
# --------------------------------------------------------------------------- #

def _isolate_wake_state(cfg, tmp_path, state: dict | None):
    from cortex import wake_state
    p = tmp_path / "wake_state.json"
    cfg["paths"]["wake_state_file"] = str(p)
    if state is not None:
        p.write_text(json.dumps(state), encoding="utf-8")
    return p


def test_gather_window_sid_override(marrow_conn, cfg, tmp_path, monkeypatch):
    """Caller-supplied window_sid wins in the gathered data even when wake_state
    carries a stale (or no) transcript — awake_since still comes from wake_state.
    Neither is rendered any more (Window line retired); the data keys stay for
    callers that pass window_sid (watchdog / note_render --transcript)."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_computer_status", lambda _cfg: None)
    since = (NOW - timedelta(minutes=3)).astimezone(ZoneInfo("UTC")).isoformat()
    _isolate_wake_state(cfg, tmp_path, {
        "transcript": "/x/deadbeef00.jsonl", "awake_since": since})

    data = note.gather(marrow_conn, cfg, NOW, window_sid="feed1234")
    assert data["window_sid"] == "feed1234"
    assert data["awake_since_hm"] == (NOW - timedelta(minutes=3)).strftime("%H:%M")
    assert "Window:" not in note.render(cfg, NOW, data)


def test_gather_window_sid_falls_back_to_wake_state(marrow_conn, cfg, tmp_path, monkeypatch):
    """No override -> Window SID comes from wake_state.transcript (legacy path)."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_computer_status", lambda _cfg: None)
    _isolate_wake_state(cfg, tmp_path, {"transcript": "/x/abcd1234ef.jsonl"})

    data = note.gather(marrow_conn, cfg, NOW)
    assert data["window_sid"] == "abcd1234"


def test_gather_window_sid_only_when_wake_state_empty(marrow_conn, cfg, tmp_path, monkeypatch):
    """Override lands in the data even with no awake_since/no state."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_computer_status", lambda _cfg: None)
    _isolate_wake_state(cfg, tmp_path, {})

    data = note.gather(marrow_conn, cfg, NOW, window_sid="cafe0001")
    assert data["window_sid"] == "cafe0001"
    assert data["awake_since_hm"] is None
    assert "Window:" not in note.render(cfg, NOW, data)


# --------------------------------------------------------------------------- #
# note_render CLI entry — fresh render, no side effects
# --------------------------------------------------------------------------- #

def test_note_render_main_prints_fresh_note_no_writes(tmp_path, monkeypatch, capsys):
    from cortex import config as _config, db as _db, note_render
    dbp = tmp_path / "marrow.db"
    _db.connect_path(dbp).close()  # create schema
    before = dbp.stat().st_mtime

    _cfg = _config.load(path=tmp_path / "absent.toml")
    _cfg["paths"]["marrow_db"] = str(dbp)
    _cfg["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    _cfg["paths"]["cortex_home"] = str(tmp_path / "home")
    monkeypatch.setattr(_config, "load", lambda path=None: _cfg)
    monkeypatch.setattr(note, "_computer_status", lambda _cfg: None)
    monkeypatch.setattr("sys.argv", ["note_render", "--transcript", "/t/feed1234ab.jsonl"])

    note_render.main()
    out = capsys.readouterr().out
    assert "AUTOMATED WAKE SIGNAL" in out  # machine tag (default on)
    assert "Now: " not in out  # Now line deleted
    assert "Window:" not in out  # Window/SID line retired
    # no wake_state written, DB not mutated by a fresh render
    assert not (tmp_path / "ws.json").exists()


def _render_cli(tmp_path, monkeypatch, capsys, argv):
    """note_render.main() over a fixed DB, returning its stdout."""
    from cortex import config as _config, db as _db, note_render
    tmp_path.mkdir(parents=True, exist_ok=True)
    dbp = tmp_path / "marrow.db"
    conn = _db.connect_path(dbp)
    make_events_table(conn)
    conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:00:00+00:00", "assistant", "tg shell self talk", "tg"),
            ("s", "2026-07-08T03:01:00+00:00", "assistant", "cli shell self talk", "ct"),
        ],
    )
    conn.commit()
    conn.close()

    _cfg = _config.load(path=tmp_path / "absent.toml")
    _cfg["paths"]["marrow_db"] = str(dbp)
    _cfg["paths"]["wake_state_file"] = str(tmp_path / "ws.json")
    _cfg["paths"]["cortex_home"] = str(tmp_path / "home")
    monkeypatch.setattr(_config, "load", lambda path=None: _cfg)
    monkeypatch.setattr(note, "_computer_status", lambda _cfg: None)
    monkeypatch.setattr("sys.argv", ["note_render", *argv])

    class _FrozenNow(datetime):          # a minute boundary must not flake this
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW

    monkeypatch.setattr(note_render, "datetime", _FrozenNow)
    note_render.main()
    return capsys.readouterr().out


def test_note_render_carries_no_replay(tmp_path, monkeypatch, capsys):
    """note_render never carries Replay — marrow's turn_inject is the single
    replay channel for every session."""
    out = _render_cli(tmp_path, monkeypatch, capsys, [])
    assert "AUTOMATED WAKE SIGNAL" in out  # machine tag (default on)
    assert "Now:" not in out  # Now line deleted
    assert "### Replay" not in out
    assert "tg shell self talk" not in out
    # the live tg bridge shape (note_render_cmd) still renders a note body
    out = _render_cli(tmp_path / "shelled", monkeypatch, capsys,
                      ["--shell", "tg"])
    assert "AUTOMATED WAKE SIGNAL" in out
    assert "Now:" not in out
    assert "### Replay" not in out


# --------------------------------------------------------------------------- #
# per-shell wake ledger (T1) + force-slept reason copy (T4)
# --------------------------------------------------------------------------- #

def test_last_wake_is_scoped_to_its_own_shell(marrow_conn):
    """T1: a cli row never surfaces for the tg shell, and vice versa — one
    shell's force-slept marker can never appear on another shell's page."""
    cli_ts = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    tg_ts = (NOW - timedelta(minutes=90)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept, shell) "
        "VALUES (?, 1, 0, ?, ?)",
        [(cli_ts, "ct-pause", "cli"), (tg_ts, None, "tg")],
    )
    marrow_conn.commit()

    tg = note._last_wake(marrow_conn, NOW, "tg")
    assert tg["minutes_ago"] == 90 and tg["force_slept"] is None
    cli = note._last_wake(marrow_conn, NOW, "cli")
    assert cli["minutes_ago"] == 30 and cli["force_slept"] == "ct-pause"
    # No shell given = the cli render (unqualified default).
    assert note._last_wake(marrow_conn, NOW)["ts"] == cli_ts


def test_last_wake_none_when_other_shell_only(marrow_conn):
    """T1: the tg page shows nothing when only cli rows exist."""
    ts = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept, shell) "
        "VALUES (?, 1, 0, 'ct-pause', 'cli')", (ts,))
    marrow_conn.commit()
    assert note._last_wake(marrow_conn, NOW, "tg") is None


def test_gather_render_scopes_wake_fallback_to_its_shell(marrow_conn, cfg):
    """T1 end-to-end: a cli wake row does not reach a tg render (the fallback
    minutes are per shell)."""
    ts = (NOW - timedelta(minutes=30)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, force_slept, shell) "
        "VALUES (?, 1, 0, 'ct-pause', 'cli')", (ts,))
    marrow_conn.commit()
    tg_text = note.render(cfg, NOW, note.gather(marrow_conn, cfg, NOW, shell="tg"))
    assert "Cortex last wake" not in tg_text
    cli_text = note.render(cfg, NOW, note.gather(marrow_conn, cfg, NOW, shell="cli"))
    assert "Cortex last wake: 30m ago" in cli_text


def test_render_never_tags_force_slept(cfg):
    """ct_wake_log.force_slept is the cli window's own shutdown ledger (only
    lie_down writes it, only for cli), so it can never be the note's pause tag —
    no force_slept value renders anything."""
    for reason in ("ct-pause", "fuse", "stale", "auto", "", None):
        text = note.render(cfg, NOW, {"last_wake": {"minutes_ago": 5,
                                                    "force_slept": reason}})
        assert "force-slept" not in text and "paused" not in text
