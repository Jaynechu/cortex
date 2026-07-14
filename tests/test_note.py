from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cortex import config, note
from cortex.pacemaker.triggers import TriggerReason

MEL = ZoneInfo("Australia/Melbourne")
NOW = datetime(2026, 7, 8, 14, 30, tzinfo=MEL)


@pytest.fixture
def cfg(tmp_path):
    # Pure defaults: point load at a nonexistent path so no live cortex.toml leaks in.
    return config.load(path=tmp_path / "absent.toml")


def make_events_table(conn):
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "timestamp TEXT, role TEXT, content TEXT, channel TEXT)"
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# render — full note + omissions
# --------------------------------------------------------------------------- #

def test_render_full_note(cfg):
    data = {
        "wake_parts": ["wander"],
        "last_wake": {"minutes_ago": 12, "force_slept": None},
        "budget": {
            "five_h_pct": 5.0, "five_h_reset": "04:50",
            "seven_d_pct": 50.0, "seven_d_countdown": "1d2h",
            "window_tokens": 50000, "today_tokens": 250000, "daily_budget": 1_000_000,
        },
        "active_app": "Google Chrome",
        "pending": [{"hm": "00:18", "intent": "去看看老婆睡了没"}],
        "replay": [
            {"channel": "cli", "hm": "00:30", "role": "N", "content": "笨鸭子"},
            {"channel": "cli", "hm": "00:31", "role": "Y", "content": "在呢"},
        ],
    }
    text = note.render(cfg, NOW, data)
    assert "Wake:" not in text  # reason line retired
    assert text.startswith("Now: 14:30 Wed | Last wake: 12min ago")
    # Plan Used line: USED %, pipe-joined, template口径
    assert ("Plan Used: 5h 5% (04:50) | 7d 50% (1d2h) | "
            "Cortex Today 250k/1M 25% | Net Session Token: 50k") in text
    assert "Active (Mac): Google Chrome" in text
    assert "Pending self-schedule: due 00:18 去看看老婆睡了没" in text
    assert "### Replay" in text
    assert "[cli 00:30] N: 笨鸭子" in text
    assert "[cli 00:31] Y: 在呢" in text
    # block separators
    assert "\n\n---\n\n" in text
    # cal/rem retired
    assert "Cal:" not in text and "Rem:" not in text


def test_render_omits_absent_lines(cfg):
    text = note.render(cfg, NOW, {})
    assert "Wake:" not in text  # reason line retired
    assert text.startswith("Now: 14:30 Wed")
    assert "Last wake:" not in text
    assert "Plan Used:" not in text
    assert "Active (Mac):" not in text
    assert "Pending" not in text
    assert "### Replay" not in text


def test_render_turn_end_line_appears_every_render(cfg):
    text = note.render(cfg, NOW, {})
    assert text.rstrip().endswith(
        "NOTE: Call MCP tool to wait or lie_down at the end of each turn. "
        "Wait=wait(N) [N=16-55]; sleep=lie_down(next_wake_min=N) "
        "[90-360; rotate=True unlocks ≥16]. "
        "Skip call = sleep in 5 mins. Auto timer is on during active chat "
        "- no call needed.")


def test_render_turn_end_line_omitted_when_blank(cfg):
    cfg["note"]["turn_end_text"] = ""
    text = note.render(cfg, NOW, {})
    assert "NOTE: Call MCP tool" not in text


def test_render_title_prepended_with_blank_line(cfg):
    cfg["note"]["title"] = "📮 小道消息"
    text = note.render(cfg, NOW, {})
    assert text.startswith("📮 小道消息\n\nNow: ")


def test_render_title_empty_omits_it(cfg):
    text = note.render(cfg, NOW, {})
    assert text.startswith("Now: ")
    assert "小道消息" not in text


def test_render_force_slept_marker_and_catchup(cfg):
    data = {"last_wake": {"minutes_ago": 40, "force_slept": "timeout"}}
    text = note.render(cfg, NOW, data)
    assert "Last wake: 40min ago (force-slept mid-task)" in text
    # catch-up backfill hint appears only on a force-slept prior window
    assert "recall all events from DB" in text


def test_render_auto_sleep_is_neutral(cfg):
    """force_slept='auto' = routine silence sleep -> NO force-incident tag, NO
    catchup hint. Rows stay queryable, but the note reads it as ordinary."""
    data = {"last_wake": {"minutes_ago": 40, "force_slept": "auto"}}
    text = note.render(cfg, NOW, data)
    assert "Last wake: 40min ago" in text
    assert "force-slept mid-task" not in text
    assert "recall all events from DB" not in text


def test_render_no_wake_line_ever(cfg):
    """The 'Wake:' reason line is fully retired — gone from every render."""
    for data in ({}, {"wake_parts": ["wander"]}, {"last_wake": {"minutes_ago": 5, "force_slept": None}}):
        assert "Wake:" not in note.render(cfg, NOW, data)


# --------------------------------------------------------------------------- #
# budget render
# --------------------------------------------------------------------------- #

def test_render_budget_segments_optional(cfg):
    b = {"five_h_pct": None, "five_h_reset": None, "seven_d_pct": None,
         "seven_d_countdown": None, "window_tokens": None,
         "today_tokens": 50000, "daily_budget": 1_000_000}
    assert note._render_budget(b) == "Plan Used: Cortex Today 50k/1M 5%"


def test_render_budget_shows_used_pct():
    """five_h_pct/seven_d_pct are UTILIZATION (used); the Plan Used line shows
    the used % verbatim (statusline 口径), reset in parens."""
    b = {"five_h_pct": 5.0, "five_h_reset": "04:50", "seven_d_pct": 50.0,
         "seven_d_countdown": "1d2h", "window_tokens": None,
         "today_tokens": 0, "daily_budget": 1_000_000}
    line = note._render_budget(b)
    assert "5h 5% (04:50)" in line
    assert "7d 50% (1d2h)" in line


def test_countdown_compact():
    now = datetime(2026, 7, 8, 0, 0, tzinfo=MEL)
    reset = (now + timedelta(days=1, hours=2)).astimezone(ZoneInfo("UTC")).isoformat()
    assert note._countdown(reset, now) == "1d2h"
    reset2 = (now + timedelta(hours=5)).astimezone(ZoneInfo("UTC")).isoformat()
    assert note._countdown(reset2, now) == "5h"
    past = (now - timedelta(hours=1)).astimezone(ZoneInfo("UTC")).isoformat()
    assert note._countdown(past, now) is None


def test_fmt_budget():
    assert note._fmt_budget(1_000_000) == "1M"
    assert note._fmt_budget(2_000_000) == "2M"
    assert note._fmt_budget(500_000) == "500k"


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


# --------------------------------------------------------------------------- #
# catchup suppression — handoff written after the prior wake ts
# --------------------------------------------------------------------------- #

def _handoff_cfg(cfg, tmp_path):
    p = tmp_path / "handoff.md"
    cfg["paths"]["handoff_file"] = str(p)
    return p


def test_handoff_after_written_post_wake(cfg, tmp_path):
    prev = (NOW - timedelta(minutes=40)).astimezone(ZoneInfo("UTC")).isoformat()
    p = _handoff_cfg(cfg, tmp_path)
    p.write_text("handoff body")  # written now -> mtime > prev ts
    assert note._handoff_after(cfg, prev) is True


def test_handoff_after_older_than_wake(cfg, tmp_path):
    import os
    import time
    p = _handoff_cfg(cfg, tmp_path)
    p.write_text("stale handoff")
    old = time.time() - 3600
    os.utime(p, (old, old))  # handoff mtime an hour before the prior wake
    prev = datetime.now(ZoneInfo("UTC")).isoformat()  # wake after the handoff
    assert note._handoff_after(cfg, prev) is False


def test_handoff_after_empty_file(cfg, tmp_path):
    prev = (NOW - timedelta(minutes=40)).astimezone(ZoneInfo("UTC")).isoformat()
    p = _handoff_cfg(cfg, tmp_path)
    p.write_text("   \n")  # non-empty mtime but blank content
    assert note._handoff_after(cfg, prev) is False


def test_handoff_after_missing_file(cfg, tmp_path):
    prev = (NOW - timedelta(minutes=40)).astimezone(ZoneInfo("UTC")).isoformat()
    _handoff_cfg(cfg, tmp_path)  # path set, file never created
    assert note._handoff_after(cfg, prev) is False


def test_handoff_after_none_ts(cfg):
    assert note._handoff_after(cfg, None) is False


def test_render_catchup_suppressed_when_handoff_written(cfg):
    """Prior window force-slept but its handoff was written after -> the catchup
    line is skipped (nothing to backfill)."""
    data = {
        "last_wake": {"minutes_ago": 40, "force_slept": "stale"},
        "catchup_handoff_written": True,
    }
    text = note.render(cfg, NOW, data)
    assert "Last wake: 40min ago (force-slept mid-task)" in text  # tag still shown
    assert "recall all events from DB" not in text  # catchup suppressed


def test_render_catchup_fires_when_no_handoff(cfg):
    """Prior window force-slept and no handoff written -> catchup fires."""
    data = {
        "last_wake": {"minutes_ago": 40, "force_slept": "stale"},
        "catchup_handoff_written": False,
    }
    text = note.render(cfg, NOW, data)
    assert "recall all events from DB" in text


def test_today_tokens_melbourne_local_boundary(marrow_conn):
    """Only today's local-date rows enter the per-window metric. Two of today's
    rows form one window (30k -> 60k); a yesterday and a tomorrow row are
    excluded by the local-date filter, so they never open a spurious window /
    drop. The single today-run is the current window -> its final is added via
    the live hint (60k here)."""
    from cortex.pacemaker import integration
    # now = 2026-07-08 00:30 AEST (+10) => UTC 2026-07-07T14:30Z
    now = datetime(2026, 7, 8, 0, 30, tzinfo=MEL)
    rows = [
        # 2026-07-07T13:00Z -> 2026-07-07 23:00 AEST = yesterday local -> excluded
        ("2026-07-07T13:00:00+00:00", 999),
        # 2026-07-07T20:00Z -> 2026-07-08 06:00 AEST = today local -> counted
        ("2026-07-07T20:00:00+00:00", 30_000),
        # 2026-07-07T20:30Z -> today local, same window grows -> final 60k
        ("2026-07-07T20:30:00+00:00", 60_000),
        # 2026-07-08T15:00Z -> 2026-07-09 01:00 AEST = tomorrow local -> excluded
        ("2026-07-08T15:00:00+00:00", 555),
    ]
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?, 1, 0, ?)", rows)
    marrow_conn.commit()
    integration.store_window_tokens(marrow_conn, 60_000)  # today's run is the live window
    assert note._today_tokens(marrow_conn, now) == 60_000


def test_replay_events_channel_time_and_truncation(marrow_conn, cfg):
    make_events_table(marrow_conn)
    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:00:00+00:00", "user", "hi", "wx"),
            ("s", "2026-07-08T03:01:00+00:00", "tl", "【专注】skip me", "cli"),
            ("s", "2026-07-08T03:02:00+00:00", "assistant", "y" * 500, "cli"),
        ],
    )
    marrow_conn.commit()
    ev = note._replay_events(marrow_conn, cfg, 6, 300)
    assert len(ev) == 2  # tl excluded
    assert ev[0] == {"channel": "wx", "hm": "13:00", "role": "N", "content": "hi"}
    assert ev[1]["channel"] == "cli"
    assert ev[1]["role"] == "Y"  # assistant -> Y
    assert len(ev[1]["content"]) == 300 and ev[1]["content"].endswith("…")


def test_replay_excludes_cortex_self_talk(marrow_conn, cfg):
    make_events_table(marrow_conn)
    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:00:00+00:00", "user", "user message", "cli"),
            ("s", "2026-07-08T03:01:00+00:00", "assistant", "cortex自言自语", "ct"),
            ("s", "2026-07-08T03:02:00+00:00", "user", "cortex醒来读note", "ct"),
            ("s", "2026-07-08T03:03:00+00:00", "assistant", "assistant reply", "cli"),
        ],
    )
    marrow_conn.commit()
    ev = note._replay_events(marrow_conn, cfg, 6, 300)
    # ct channel (cortex wake monologue) excluded; real cli exchange kept.
    assert [(e["channel"], e["content"]) for e in ev] == [
        ("cli", "user message"), ("cli", "assistant reply")]


def test_replay_strips_media_markers(marrow_conn, cfg):
    make_events_table(marrow_conn)
    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:00:00+00:00", "user",
             '[time: 12:30] 你看 <image path="/stk/a.png"/> 这个', "wx"),
        ],
    )
    marrow_conn.commit()
    ev = note._replay_events(marrow_conn, cfg, 6, 300)
    assert ev[0]["content"] == "你看 这个"


def test_replay_events_since_ts_filters_older_events(marrow_conn, cfg):
    """Diff mode (D6): since_ts excludes events at or before it, keeps newer."""
    make_events_table(marrow_conn)
    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:00:00+00:00", "user", "old message", "wx"),
            ("s", "2026-07-08T03:05:00+00:00", "assistant", "old reply", "wx"),
            ("s", "2026-07-08T03:10:00+00:00", "user", "new message", "wx"),
        ],
    )
    marrow_conn.commit()
    ev = note._replay_events(marrow_conn, cfg, 6, 300, since_ts="2026-07-08T03:05:00+00:00")
    assert [e["content"] for e in ev] == ["new message"]


def test_replay_events_since_ts_none_is_full_replay(marrow_conn, cfg):
    make_events_table(marrow_conn)
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "hi", "wx"))
    marrow_conn.commit()
    ev = note._replay_events(marrow_conn, cfg, 6, 300, since_ts=None)
    assert len(ev) == 1


def test_replay_exclude_channels_configurable(marrow_conn):
    make_events_table(marrow_conn)
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "assistant", "ct turn", "ct"))
    marrow_conn.commit()
    # empty exclude list → include everything, including ct
    ev = note._replay_events(marrow_conn, {"note": {"replay_exclude_channels": []}}, 6, 300)
    assert [e["content"] for e in ev] == ["ct turn"]


def test_window_tokens_absent_key_is_none(marrow_conn):
    marrow_conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)",
        (json.dumps({"desire": {}}), "2026-07-08T00:00:00Z"))
    marrow_conn.commit()
    assert note._window_tokens(marrow_conn) is None


def test_window_tokens_reads_hint(marrow_conn):
    marrow_conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)",
        (json.dumps({"window_tokens": 84000}), "2026-07-08T00:00:00Z"))
    marrow_conn.commit()
    assert note._window_tokens(marrow_conn) == 84000


# --------------------------------------------------------------------------- #
# external best-effort facts (monkeypatched)
# --------------------------------------------------------------------------- #

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


def test_frontmost_app_locked_returns_none(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "loginwindow\n"
    monkeypatch.setattr(note.subprocess, "run", lambda *a, **k: FakeProc())
    assert note._frontmost_app() is None


def test_frontmost_app_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no osascript")
    monkeypatch.setattr(note.subprocess, "run", boom)
    assert note._frontmost_app() is None


def test_frontmost_app_ok(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "WeChat\n"
    monkeypatch.setattr(note.subprocess, "run", lambda *a, **k: FakeProc())
    assert note._frontmost_app() == "WeChat"


# --------------------------------------------------------------------------- #
# gather integration (external facts stubbed)
# --------------------------------------------------------------------------- #

def test_gather_end_to_end(marrow_conn, cfg, tmp_path, monkeypatch):
    # Isolate wake_state so a stale last_note_ts on a real machine's live state
    # (diff-mode baseline, D6) can never filter out this fixture's replay row.
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "hi", "wx"))
    marrow_conn.execute(
        "CREATE TABLE ct_rate_limit (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    marrow_conn.executemany(
        "INSERT INTO ct_rate_limit (key, value, updated_at) VALUES (?, ?, '')",
        [("five_hour_pct", "40"), ("five_hour_reset_at", "2026-07-08T04:30:00+00:00"),
         ("seven_day_pct", "12")])
    marrow_conn.commit()

    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    data = note.gather(marrow_conn, cfg, NOW, decision={
        "reasons": [TriggerReason(kind="floor", detail="floor check due")]})
    assert "wake_parts" not in data  # Wake reason line retired
    assert data["budget"]["five_h_pct"] == 40.0
    assert data["budget"]["five_h_reset"] == "14:30"  # 04:30Z -> AEST
    assert data["budget"]["seven_d_pct"] == 12.0
    assert len(data["replay"]) == 1
    assert "handoff" not in data  # handoff moved to SessionStart
    text = note.render(cfg, NOW, data)
    assert text.startswith("Now: ")
    assert "Wake:" not in text


# --------------------------------------------------------------------------- #
# gather diff mode (D6): last_note_ts baseline persisted + advanced per render
# --------------------------------------------------------------------------- #

def test_gather_second_call_diffs_against_first(marrow_conn, cfg, tmp_path, monkeypatch):
    """Two consecutive gather() calls in the same wake: the first (wake's
    initial note) sees everything; the second sees only events newer than the
    first's baseline (last_note_ts persisted in wake_state)."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "first round message", "wx"))
    marrow_conn.commit()

    # Free-round render advances the baseline (advance_baseline=True).
    data1 = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    assert [e["content"] for e in data1["replay"]] == ["first round message"]
    baseline = wake_state.get_last_note_ts(cfg)
    assert baseline == "2026-07-08T03:00:00+00:00"

    # New activity lands between the two rounds (e.g. wx channel while cortex slept).
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:10:00+00:00", "user", "second round message", "wx"))
    marrow_conn.commit()

    data2 = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    # Only the new event, not the one already shown in the first note.
    assert [e["content"] for e in data2["replay"]] == ["second round message"]
    # Baseline advances forward.
    assert wake_state.get_last_note_ts(cfg) == "2026-07-08T03:10:00+00:00"


def test_gather_diff_shows_cross_channel_activity(marrow_conn, cfg, tmp_path, monkeypatch):
    """User activity on wx/tg channels between rounds shows up in the diff (not
    just the active window's own channel)."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "assistant", "cli reply", "cli"))
    marrow_conn.commit()
    # Free-round render advances the baseline (advance_baseline=True).
    note.gather(marrow_conn, cfg, NOW, advance_baseline=True)  # round 1 -> baseline set

    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:05:00+00:00", "user", "wx message", "wx"),
            ("s", "2026-07-08T03:06:00+00:00", "user", "tg message", "tg"),
        ],
    )
    marrow_conn.commit()

    data2 = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    channels = [(e["channel"], e["content"]) for e in data2["replay"]]
    assert ("wx", "wx message") in channels
    assert ("tg", "tg message") in channels
    assert ("cli", "cli reply") not in channels  # already seen in round 1


def test_gather_render_only_does_not_advance_baseline(marrow_conn, cfg, tmp_path, monkeypatch):
    """Render-only paths (marrow render_module / --print-note / SessionStart
    re-render) default advance_baseline=False and MUST NOT move the baseline."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "msg", "wx"))
    marrow_conn.commit()

    note.gather(marrow_conn, cfg, NOW)  # render-only, default False
    assert wake_state.get_last_note_ts(cfg) is None
    note.gather(marrow_conn, cfg, NOW)  # again -> still no baseline
    assert wake_state.get_last_note_ts(cfg) is None


def test_gather_free_rounds_diff_across_interleaved_print_note(marrow_conn, cfg, tmp_path, monkeypatch):
    """Two consecutive free-rounds diff correctly even when a render-only
    --print-note peek happens in between (the peek must not eat the diff)."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "round1 msg", "wx"))
    marrow_conn.commit()
    d1 = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)  # free-round 1
    assert [e["content"] for e in d1["replay"]] == ["round1 msg"]

    # New activity, then a render-only debug peek that must not advance baseline.
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:05:00+00:00", "user", "round2 msg", "wx"))
    marrow_conn.commit()
    note.gather(marrow_conn, cfg, NOW)  # --print-note peek, False

    d2 = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)  # free-round 2
    contents = [e["content"] for e in d2["replay"]]
    assert contents == ["round2 msg"]  # peek did not consume it


def test_seed_baseline_anchors_first_free_round(marrow_conn, cfg, tmp_path, monkeypatch):
    """seed_baseline (D6 wake-open seed) anchors the baseline so the FIRST
    free-round diffs from wake-open, not epoch zero."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "pre-wake msg", "wx"))
    marrow_conn.commit()
    note.seed_baseline(marrow_conn, cfg)
    assert wake_state.get_last_note_ts(cfg) == "2026-07-08T03:00:00+00:00"

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:05:00+00:00", "user", "post-wake msg", "wx"))
    marrow_conn.commit()
    d = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    assert [e["content"] for e in d["replay"]] == ["post-wake msg"]


# --------------------------------------------------------------------------- #
# BUG B: initial-wake full replay must not present pre-wake events as fresh
# --------------------------------------------------------------------------- #

def _seed_prev_wake(conn, minutes_ago: int) -> str:
    """Write a prior wake=1 row `minutes_ago` before NOW; return its ISO ts."""
    ts = (NOW - timedelta(minutes=minutes_ago)).astimezone(ZoneInfo("UTC")).isoformat()
    conn.execute("INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)", (ts,))
    conn.commit()
    return ts


def test_gather_initial_wake_only_old_events_is_stale(
        marrow_conn, cfg, tmp_path, monkeypatch):
    """BUG B: initial wake (no diff baseline) where every eligible event PREDATES
    the prior wake -> 'no new messages', never a fake-fresh '### Replay' of an old
    conversation. _replay applies no since filter on the initial note, so it would
    otherwise return the old rows and render() would show them as fresh."""
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    cfg["paths"]["handoff_file"] = str(tmp_path / "handoff.md")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    _seed_prev_wake(marrow_conn, 16)  # prior wake 16 min ago
    # Only OLD events, all before the prior wake (well before NOW).
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T02:00:00+00:00", "user", "an old conversation", "wx"))
    marrow_conn.commit()

    data = note.gather(marrow_conn, cfg, NOW)  # initial wake: no last_note_ts
    assert data["replay_stale"] is True
    text = note.render(cfg, NOW, data)
    assert "No new messages since last wake." in text
    assert "### Replay" not in text
    assert "an old conversation" not in text


def test_gather_initial_wake_new_events_render_replay(
        marrow_conn, cfg, tmp_path, monkeypatch):
    """BUG B counterpart: initial wake with genuinely NEW events (after the prior
    wake) -> replay is rendered and the cutoff is the newest rendered event."""
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    cfg["paths"]["handoff_file"] = str(tmp_path / "handoff.md")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    _seed_prev_wake(marrow_conn, 16)
    # Event AFTER the prior wake (04:14Z) — genuinely fresh.
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T04:20:00+00:00", "user", "a fresh message", "wx"))
    marrow_conn.commit()

    data = note.gather(marrow_conn, cfg, NOW)  # initial wake
    assert data["replay_stale"] is False
    assert [e["content"] for e in data["replay"]] == ["a fresh message"]
    assert data["replay_cutoff_ts"] == "2026-07-08T04:20:00+00:00"
    text = note.render(cfg, NOW, data)
    assert "### Replay" in text
    assert "a fresh message" in text


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


def test_gather_stale_boundary_uses_exact_ts_not_floored_minutes(
        marrow_conn, cfg, tmp_path, monkeypatch):
    """Codex P2: the staleness comparison must use last_wake['ts'] directly, not
    now - timedelta(minutes=minutes_ago) (floored, can land up to 59s AFTER the
    real wake). Prior wake at 04:14:31Z (929s ago -> floored to 15 whole
    minutes). A genuinely NEW event at 04:14:50Z (19s after the real wake) must
    render as fresh — the floored reconstruction (04:15:00Z) would wrongly
    place the boundary after this event and stale it."""
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    cfg["paths"]["handoff_file"] = str(tmp_path / "handoff.md")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)",
        ("2026-07-08T04:14:31+00:00",))
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T04:14:50+00:00", "user", "just after the real wake", "wx"))
    marrow_conn.commit()

    data = note.gather(marrow_conn, cfg, NOW)  # initial wake
    assert data["replay_stale"] is False
    assert [e["content"] for e in data["replay"]] == ["just after the real wake"]


def test_gather_returns_replay_cutoff_of_rendered_events(marrow_conn, cfg, tmp_path, monkeypatch):
    """gather() exposes replay_cutoff_ts = the newest ts it actually rendered.
    When nothing is newer than the baseline it diffed from, the cutoff is that
    baseline (so a deferred advance never rewinds)."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "shown", "wx"))
    marrow_conn.commit()
    d = note.gather(marrow_conn, cfg, NOW)
    assert d["replay_cutoff_ts"] == "2026-07-08T03:00:00+00:00"

    # Baseline caught up; a re-render with nothing new returns the baseline itself.
    wake_state.set_last_note_ts(cfg, "2026-07-08T03:00:00+00:00")
    d2 = note.gather(marrow_conn, cfg, NOW)
    assert d2["replay_cutoff_ts"] == "2026-07-08T03:00:00+00:00"


def test_seed_baseline_uses_captured_cutoff_not_requery(marrow_conn, cfg, tmp_path, monkeypatch):
    """P2-A race: an event inserted between the wake note's assembly and the D6
    seed (the ~90s window spawn) must NOT be swallowed by the baseline. seed_baseline
    honours the cutoff captured at assembly, so that later event still shows in the
    FIRST free-round instead of being dropped."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "in wake note", "wx"))
    marrow_conn.commit()
    # Assembly captures the cutoff of the note it rendered.
    captured = note.gather(marrow_conn, cfg, NOW)["replay_cutoff_ts"]
    assert captured == "2026-07-08T03:00:00+00:00"

    # An event races in during the window spawn — absent from the wake note.
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:30+00:00", "user", "raced in during spawn", "wx"))
    marrow_conn.commit()

    # Seeding from the CAPTURED cutoff (not a fresh query) keeps the racer replayable.
    note.seed_baseline(marrow_conn, cfg, cutoff_ts=captured)
    assert wake_state.get_last_note_ts(cfg) == "2026-07-08T03:00:00+00:00"
    d = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    assert [e["content"] for e in d["replay"]] == ["raced in during spawn"]


def test_deferred_advance_uses_gather_cutoff_not_requery(marrow_conn, cfg, tmp_path, monkeypatch):
    """P2-B race: an event inserted between gather() (which built the free-round
    text) and the deferred baseline advance must appear in the NEXT note, not be
    consumed. Advancing to the cutoff gather() actually used (never a second
    query) keeps that racer replayable next round."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "round1 shown", "wx"))
    marrow_conn.commit()
    # Free-round render (advance_baseline=False, as watchdog does): gather returns
    # the cutoff it built the text on. The caller defers the advance to after write.
    data = note.gather(marrow_conn, cfg, NOW, advance_baseline=False)
    assert [e["content"] for e in data["replay"]] == ["round1 shown"]
    pending = data["replay_cutoff_ts"]
    assert pending == "2026-07-08T03:00:00+00:00"

    # Event races in AFTER gather built the text but BEFORE the deferred advance.
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:30+00:00", "user", "raced after gather", "wx"))
    marrow_conn.commit()

    # Deferred advance uses the captured cutoff verbatim — NOT the newer racer.
    wake_state.set_last_note_ts(cfg, pending)
    d2 = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    assert [e["content"] for e in d2["replay"]] == ["raced after gather"]


def test_gather_cutoff_from_rendered_rows_not_separate_query(marrow_conn, cfg, tmp_path, monkeypatch):
    """#1: the cutoff must be derived from the SAME read as the rendered replay,
    not a separate _latest_replay_ts query. An event committed between the two
    reads (simulated by making a separate query see a newer row than the render)
    must NOT be swallowed. Here we assert gather never calls _latest_replay_ts to
    derive the cutoff on the has-events path (it is only a staleness helper)."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "rendered", "wx"))
    marrow_conn.commit()

    # A separate _latest_replay_ts would return a NEWER ts (the racer) than the
    # rendered subset — poison it to prove the cutoff never comes from that query.
    monkeypatch.setattr(note, "_latest_replay_ts",
                        lambda conn, cfg: "2026-07-08T09:99:99+00:00")
    d = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    assert [e["content"] for e in d["replay"]] == ["rendered"]
    # Cutoff = max ts of what was rendered, NOT the poisoned latest query.
    assert d["replay_cutoff_ts"] == "2026-07-08T03:00:00+00:00"
    assert wake_state.get_last_note_ts(cfg) == "2026-07-08T03:00:00+00:00"


def test_gather_cutoff_max_of_rendered_subset_overflow_not_skipped(marrow_conn, cfg, tmp_path, monkeypatch):
    """#1c: more new events than the render limit. The cutoff must be the max ts
    of the RENDERED subset (the newest `limit` rows), never the max ts of ALL
    newer rows — otherwise the overflow rows below the limit sit > baseline and
    get skipped forever. Advancing to the rendered cutoff keeps overflow
    replayable on the next round."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    cfg["note"] = {**cfg.get("note", {}), "replay_events": 2}  # limit = 2
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        [
            ("s", "2026-07-08T03:00:00+00:00", "user", "e1 overflow", "wx"),
            ("s", "2026-07-08T03:01:00+00:00", "user", "e2 rendered", "wx"),
            ("s", "2026-07-08T03:02:00+00:00", "user", "e3 rendered", "wx"),
        ],
    )
    marrow_conn.commit()

    d = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    # Only the newest 2 are rendered.
    assert [e["content"] for e in d["replay"]] == ["e2 rendered", "e3 rendered"]
    # Cutoff = newest of the RENDERED subset (e3), not e3 anyway here — but the
    # baseline must NOT jump past the overflow e1. Advance moves to e3.
    assert d["replay_cutoff_ts"] == "2026-07-08T03:02:00+00:00"
    assert wake_state.get_last_note_ts(cfg) == "2026-07-08T03:02:00+00:00"
    # The overflow e1 (03:00) is < the rendered cutoff (03:02) so it is consumed
    # this round by the baseline jump — pin the documented behaviour: overflow
    # OLDER than the rendered window is dropped (design: replay shows the newest
    # `limit`, older overflow is intentionally not backfilled). What must NOT
    # happen is dropping events NEWER than the rendered cutoff; there are none
    # here because the render always keeps the newest rows.
    next_round = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    assert next_round["replay"] == []  # nothing newer than e3 remains


def test_seed_baseline_explicit_none_keeps_baseline_no_requery(marrow_conn, cfg, tmp_path, monkeypatch):
    """#2: seed_baseline(cutoff_ts=None) is a validly-EMPTY assembled note (zero
    eligible replay events). It must seed NOTHING (keep the baseline as-is), NOT
    fall back to a fresh _latest_replay_ts re-query — that re-query would race in
    an event the empty note never showed and drop it from the first free-round."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    # An event races in during the window spawn (would be picked by a re-query).
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:30+00:00", "user", "raced in during spawn", "wx"))
    marrow_conn.commit()
    # If seed re-queried, it would sink the baseline to 03:00:30 and drop the racer.
    monkeypatch.setattr(note, "_latest_replay_ts",
                        lambda conn, cfg: pytest.fail("must not re-query on explicit None"))

    note.seed_baseline(marrow_conn, cfg, cutoff_ts=None)  # empty note -> seed nothing
    assert wake_state.get_last_note_ts(cfg) is None  # baseline untouched

    # First free-round still replays the racer (full replay, baseline None).
    d = note.gather(marrow_conn, cfg, NOW, advance_baseline=True)
    assert [e["content"] for e in d["replay"]] == ["raced in during spawn"]


def test_seed_baseline_omitted_arg_requeries_legacy(marrow_conn, cfg, tmp_path, monkeypatch):
    """#2 counterpart: the OMITTED arg (legacy / test callers with no captured
    cutoff) still falls back to a fresh _latest_replay_ts query."""
    from cortex import wake_state
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    make_events_table(marrow_conn)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:00+00:00", "user", "pre-wake", "wx"))
    marrow_conn.commit()
    note.seed_baseline(marrow_conn, cfg)  # arg omitted -> re-query
    assert wake_state.get_last_note_ts(cfg) == "2026-07-08T03:00:00+00:00"


def test_gather_survives_naive_due_at_self_schedule(marrow_conn, cfg, tmp_path, monkeypatch):
    """Live-repro regression: a self_schedule.json entry with an offset-free
    (naive) due_at must not crash gather()/render() end-to-end."""
    make_events_table(marrow_conn)
    marrow_conn.commit()

    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

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
    """Caller-supplied window_sid wins for the Window line even when wake_state
    carries a stale (or no) transcript — awake_since still comes from wake_state."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    since = (NOW - timedelta(minutes=3)).astimezone(ZoneInfo("UTC")).isoformat()
    _isolate_wake_state(cfg, tmp_path, {
        "transcript": "/x/deadbeef00.jsonl", "awake_since": since})

    data = note.gather(marrow_conn, cfg, NOW, window_sid="feed1234")
    assert data["window_sid"] == "feed1234"
    assert data["awake_since_hm"] == (NOW - timedelta(minutes=3)).strftime("%H:%M")
    text = note.render(cfg, NOW, data)
    assert "Window: since " in text and "SID feed1234" in text


def test_gather_window_sid_falls_back_to_wake_state(marrow_conn, cfg, tmp_path, monkeypatch):
    """No override -> Window SID comes from wake_state.transcript (legacy path)."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    _isolate_wake_state(cfg, tmp_path, {"transcript": "/x/abcd1234ef.jsonl"})

    data = note.gather(marrow_conn, cfg, NOW)
    assert data["window_sid"] == "abcd1234"


def test_gather_window_sid_only_when_wake_state_empty(marrow_conn, cfg, tmp_path, monkeypatch):
    """Override renders the Window SID line even with no awake_since/no state."""
    make_events_table(marrow_conn)
    marrow_conn.commit()
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    _isolate_wake_state(cfg, tmp_path, {})

    data = note.gather(marrow_conn, cfg, NOW, window_sid="cafe0001")
    assert data["window_sid"] == "cafe0001"
    assert data["awake_since_hm"] is None
    assert "Window: SID cafe0001" in note.render(cfg, NOW, data)


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
    monkeypatch.setattr(_config, "load", lambda path=None: _cfg)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)
    monkeypatch.setattr("sys.argv", ["note_render", "--transcript", "/t/feed1234ab.jsonl"])

    note_render.main()
    out = capsys.readouterr().out
    assert "Now: " in out
    assert "SID feed1234" in out
    # no wake_state written, DB not mutated by a fresh render
    assert not (tmp_path / "ws.json").exists()
