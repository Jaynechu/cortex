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
    assert "Wake: wander" in text
    assert "Now: 14:30 Wed | Last wake: 12min ago" in text
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
    text = note.render(cfg, NOW, {"wake_parts": ["wander"]})
    assert text.startswith("Wake: wander")
    assert "Now: 14:30 Wed" in text
    assert "Last wake:" not in text
    assert "Plan Used:" not in text
    assert "Active (Mac):" not in text
    assert "Pending" not in text
    assert "### Replay" not in text


def test_render_turn_end_line_appears_every_render(cfg):
    text = note.render(cfg, NOW, {"wake_parts": ["wander"]})
    assert text.rstrip().endswith(
        "NOTE: choose wait time or next wake time at the end of each turn. "
        "Wait: empty (default) / wait(N) [N=11-55]; sleep: "
        "lie_down(next_wake_min=N, or omit = dice). Pls leave empty during "
        "casual chat with user.")


def test_render_turn_end_line_omitted_when_blank(cfg):
    cfg["note"]["turn_end_text"] = ""
    text = note.render(cfg, NOW, {"wake_parts": ["wander"]})
    assert "NOTE: choose wait time" not in text


def test_render_title_prepended_with_blank_line(cfg):
    cfg["note"]["title"] = "📮 小道消息"
    text = note.render(cfg, NOW, {"wake_parts": ["wander"]})
    assert text.startswith("📮 小道消息\n\nWake: wander")


def test_render_title_empty_omits_it(cfg):
    text = note.render(cfg, NOW, {"wake_parts": ["wander"]})
    assert text.startswith("Wake: wander")
    assert "小道消息" not in text


def test_render_force_slept_marker_and_catchup(cfg):
    data = {"wake_parts": ["wander"], "last_wake": {"minutes_ago": 40, "force_slept": "timeout"}}
    text = note.render(cfg, NOW, data)
    assert "Last wake: 40min ago (force-slept mid-task)" in text
    # catch-up backfill hint appears only on a force-slept prior window
    assert "recall all events from DB" in text


# --------------------------------------------------------------------------- #
# wake line mapping
# --------------------------------------------------------------------------- #

def test_wake_parts_floor():
    d = {"reasons": [TriggerReason(kind="floor", detail="floor check due")]}
    assert note._wake_parts(d) == ["wander"]


def test_wake_parts_self_scheduled_uses_intent():
    d = {"reasons": [TriggerReason(kind="self_scheduled", detail="x",
                                   facts={"intent": "问午饭吃了什么"})]}
    assert note._wake_parts(d) == ["self-scheduled(问午饭吃了什么)"]


def test_wake_parts_schedule_uses_name():
    d = {"reasons": [TriggerReason(kind="schedule", detail="x", facts={"name": "week para"})]}
    assert note._wake_parts(d) == ["scheduled(week para)"]


def test_wake_parts_none_defaults():
    assert note._wake_parts(None) == ["wander"]
    assert note._wake_parts({"reasons": [], "explanation": "manual --force"}) == ["manual --force"]


def test_wake_parts_dict_reason():
    d = {"reasons": [{"kind": "floor", "detail": "floor check due"}]}
    assert note._wake_parts(d) == ["wander"]


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
    assert lw == {"minutes_ago": 30, "force_slept": "timeout"}


def test_last_wake_none_when_only_current(marrow_conn):
    cur = (NOW - timedelta(seconds=5)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)", (cur,))
    marrow_conn.commit()
    assert note._last_wake(marrow_conn, NOW) is None


def test_today_tokens_melbourne_local_boundary(marrow_conn):
    # now = 2026-07-08 00:30 AEST (+10) => UTC 2026-07-07T14:30Z
    now = datetime(2026, 7, 8, 0, 30, tzinfo=MEL)
    rows = [
        # 2026-07-07T20:00Z -> 2026-07-08 06:00 AEST = today local -> counted
        ("2026-07-07T20:00:00+00:00", 100),
        # 2026-07-07T13:00Z -> 2026-07-07 23:00 AEST = yesterday local -> excluded
        ("2026-07-07T13:00:00+00:00", 999),
        # 2026-07-08T15:00Z -> 2026-07-09 01:00 AEST = tomorrow local -> excluded
        ("2026-07-08T15:00:00+00:00", 555),
    ]
    marrow_conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?, 1, 0, ?)", rows)
    marrow_conn.commit()
    assert note._today_tokens(marrow_conn, now) == 100


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

def test_gather_end_to_end(marrow_conn, cfg, monkeypatch):
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
    assert data["wake_parts"] == ["wander"]
    assert data["budget"]["five_h_pct"] == 40.0
    assert data["budget"]["five_h_reset"] == "14:30"  # 04:30Z -> AEST
    assert data["budget"]["seven_d_pct"] == 12.0
    assert len(data["replay"]) == 1
    assert "handoff" not in data  # handoff moved to SessionStart
    text = note.render(cfg, NOW, data)
    assert text.startswith("Wake: wander")


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
