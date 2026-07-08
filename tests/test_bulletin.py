from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from cortex import bulletin, config
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
        "wake_parts": ["巡回"],
        "last_wake": {"minutes_ago": 12, "force_slept": None},
        "budget": {
            "five_h_pct": 42.0, "five_h_reset": "14:30", "seven_d_pct": 15.0,
            "window_tokens": 26000, "today_tokens": 123000, "daily_budget": 1_000_000,
        },
        "active_app": "WeChat",
        "cal": {"current": "Grand round", "next": "Gym"},
        "rem_last_done": "剪脚指甲",
        "pending": [{"hm": "14:40", "intent": "问午饭"}],
        "handoff": "三点半，老婆在学习。",
        "handoff_title": "阿屿の碎碎念",
        "replay": [{"channel": "wx", "hm": "14:04", "content": "笨鸭子"}],
        "replay_title": "最近对话回放",
    }
    text = bulletin.render(cfg, NOW, data)
    assert "Wake: 巡回" in text
    assert "Now: 14:30 Wed | Last wake: 12min ago" in text
    assert "Budget: 5h 42% (reset 14:30) · 7d 15% · window 26k · today 123k/1M 12%" in text
    assert "Active (Mac): WeChat" in text
    assert "Cal: Current Grand round | Next Gym" in text
    assert "Rem: 剪脚指甲" in text
    assert "Pending self-schedule: due 14:40 问午饭" in text
    assert "阿屿の碎碎念: 三点半，老婆在学习。" in text
    assert "最近对话回放:" in text
    assert "  [wx 14:04] 笨鸭子" in text


def test_render_omits_absent_lines(cfg):
    text = bulletin.render(cfg, NOW, {"wake_parts": ["巡回"]})
    assert text.startswith("Wake: 巡回")
    assert "Now: 14:30 Wed" in text
    assert "Last wake:" not in text
    assert "Budget:" not in text
    assert "Active (Mac):" not in text
    assert "Cal:" not in text
    assert "Rem:" not in text
    assert "Pending" not in text
    assert "阿屿" not in text
    assert "最近对话回放" not in text


def test_render_force_slept_marker(cfg):
    data = {"wake_parts": ["巡回"], "last_wake": {"minutes_ago": 40, "force_slept": "timeout"}}
    text = bulletin.render(cfg, NOW, data)
    assert "Last wake: 40min ago (force-slept mid-task)" in text


def test_render_cal_partial(cfg):
    text = bulletin.render(cfg, NOW, {"wake_parts": ["巡回"], "cal": {"current": None, "next": "Gym"}})
    assert "Cal: Next Gym" in text
    assert "Current" not in text


def test_render_no_whole_note_truncation(cfg):
    # Old max_chars cap removed: a long handoff is not clipped.
    long = "字" * 5000
    text = bulletin.render(cfg, NOW, {"wake_parts": ["巡回"], "handoff": long,
                                      "handoff_title": "阿屿の碎碎念"})
    assert long in text


# --------------------------------------------------------------------------- #
# wake line mapping
# --------------------------------------------------------------------------- #

def test_wake_parts_floor():
    d = {"reasons": [TriggerReason(kind="floor", detail="floor check due")]}
    assert bulletin._wake_parts(d) == ["巡回"]


def test_wake_parts_self_scheduled_uses_intent():
    d = {"reasons": [TriggerReason(kind="self_scheduled", detail="x",
                                   facts={"intent": "问午饭吃了什么"})]}
    assert bulletin._wake_parts(d) == ["Self-schedule(问午饭吃了什么)"]


def test_wake_parts_schedule_uses_name():
    d = {"reasons": [TriggerReason(kind="schedule", detail="x", facts={"name": "week para"})]}
    assert bulletin._wake_parts(d) == ["Schedule(week para)"]


def test_wake_parts_none_defaults():
    assert bulletin._wake_parts(None) == ["巡回"]
    assert bulletin._wake_parts({"reasons": [], "explanation": "manual --force"}) == ["manual --force"]


def test_wake_parts_dict_reason():
    d = {"reasons": [{"kind": "floor", "detail": "floor check due"}]}
    assert bulletin._wake_parts(d) == ["巡回"]


# --------------------------------------------------------------------------- #
# budget render
# --------------------------------------------------------------------------- #

def test_render_budget_segments_optional(cfg):
    b = {"five_h_pct": None, "five_h_reset": None, "seven_d_pct": None,
         "window_tokens": None, "today_tokens": 50000, "daily_budget": 1_000_000}
    assert bulletin._render_budget(b) == "Budget: today 50k/1M 5%"


def test_fmt_budget():
    assert bulletin._fmt_budget(1_000_000) == "1M"
    assert bulletin._fmt_budget(2_000_000) == "2M"
    assert bulletin._fmt_budget(500_000) == "500k"


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
    lw = bulletin._last_wake(marrow_conn, NOW)
    assert lw == {"minutes_ago": 30, "force_slept": "timeout"}


def test_last_wake_none_when_only_current(marrow_conn):
    cur = (NOW - timedelta(seconds=5)).astimezone(ZoneInfo("UTC")).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run) VALUES (?, 1, 0)", (cur,))
    marrow_conn.commit()
    assert bulletin._last_wake(marrow_conn, NOW) is None


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
    assert bulletin._today_tokens(marrow_conn, now) == 100


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
    ev = bulletin._replay_events(marrow_conn, cfg, 6, 300)
    assert len(ev) == 2  # tl excluded
    assert ev[0] == {"channel": "wx", "hm": "13:00", "content": "hi"}
    assert ev[1]["channel"] == "cli"
    assert len(ev[1]["content"]) == 300 and ev[1]["content"].endswith("…")


def test_window_tokens_absent_key_is_none(marrow_conn):
    marrow_conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)",
        (json.dumps({"desire": {}}), "2026-07-08T00:00:00Z"))
    marrow_conn.commit()
    assert bulletin._window_tokens(marrow_conn) is None


def test_window_tokens_reads_hint(marrow_conn):
    marrow_conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)",
        (json.dumps({"window_tokens": 84000}), "2026-07-08T00:00:00Z"))
    marrow_conn.commit()
    assert bulletin._window_tokens(marrow_conn) == 84000


# --------------------------------------------------------------------------- #
# external best-effort facts (monkeypatched)
# --------------------------------------------------------------------------- #

def test_read_handoff_gated_by_fresh_and_kind(cfg, tmp_path, monkeypatch):
    hp = tmp_path / "handoff.md"
    hp.write_text("碎碎念内容", encoding="utf-8")
    monkeypatch.setattr(config, "handoff_path", lambda c: hp)
    # not fresh -> omitted
    assert bulletin._read_handoff(cfg, fresh=False, wake_kind="rebirth") is None
    # fresh + allowed kind -> included
    assert bulletin._read_handoff(cfg, fresh=True, wake_kind="rebirth") == "碎碎念内容"
    # fresh + excluded kind -> omitted
    assert bulletin._read_handoff(cfg, fresh=True, wake_kind="resume") is None
    # fresh + kind unknown to gate (None) -> included
    assert bulletin._read_handoff(cfg, fresh=True, wake_kind=None) == "碎碎念内容"


def test_read_handoff_missing_file(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "handoff_path", lambda c: tmp_path / "nope.md")
    assert bulletin._read_handoff(cfg, fresh=True, wake_kind="rebirth") is None


def test_pending_within_window(cfg, tmp_path, monkeypatch):
    sp = tmp_path / "ss.json"
    due_soon = (NOW + timedelta(minutes=10)).isoformat()
    due_far = (NOW + timedelta(minutes=40)).isoformat()
    sp.write_text(json.dumps([
        {"due_at": due_soon, "intent": "喝水"},
        {"due_at": due_far, "intent": "太远"},
    ]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)
    pend = bulletin._pending(cfg, NOW)
    assert pend == [{"hm": (NOW + timedelta(minutes=10)).strftime("%H:%M"), "intent": "喝水"}]


def test_pending_missing_file(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "self_schedule_path", lambda c: tmp_path / "nope.json")
    assert bulletin._pending(cfg, NOW) == []


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
    pend = bulletin._pending(cfg, NOW)
    assert pend == [
        {"hm": (NOW + timedelta(minutes=5)).strftime("%H:%M"), "intent": "naive"},
        {"hm": (NOW + timedelta(minutes=10)).strftime("%H:%M"), "intent": "aware"},
    ]


def test_frontmost_app_locked_returns_none(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "loginwindow\n"
    monkeypatch.setattr(bulletin.subprocess, "run", lambda *a, **k: FakeProc())
    assert bulletin._frontmost_app() is None


def test_frontmost_app_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise OSError("no osascript")
    monkeypatch.setattr(bulletin.subprocess, "run", boom)
    assert bulletin._frontmost_app() is None


def test_frontmost_app_ok(monkeypatch):
    class FakeProc:
        returncode = 0
        stdout = "WeChat\n"
    monkeypatch.setattr(bulletin.subprocess, "run", lambda *a, **k: FakeProc())
    assert bulletin._frontmost_app() == "WeChat"


def test_cal_line_picks_current_and_next_skips_all_day(cfg, monkeypatch):
    events = [
        {"all_day": True, "title": "birthday", "start": "2026-07-08T00:00:00+10:00",
         "end": "2026-07-09T00:00:00+10:00"},
        {"all_day": False, "title": "Grand round", "start": "2026-07-08T14:00:00+10:00",
         "end": "2026-07-08T15:00:00+10:00"},
        {"all_day": False, "title": "Gym", "start": "2026-07-08T18:00:00+10:00",
         "end": "2026-07-08T19:00:00+10:00"},
    ]
    monkeypatch.setattr(bulletin, "_cadence_json", lambda c, a: events)
    assert bulletin._cal_line(cfg, NOW) == {"current": "Grand round", "next": "Gym"}


def test_cal_line_none_when_no_timed_events(cfg, monkeypatch):
    monkeypatch.setattr(bulletin, "_cadence_json", lambda c, a: [
        {"all_day": True, "title": "x", "start": "2026-07-08T00:00:00+10:00",
         "end": "2026-07-09T00:00:00+10:00"}])
    assert bulletin._cal_line(cfg, NOW) is None


def test_cal_line_cadence_unavailable(cfg, monkeypatch):
    monkeypatch.setattr(bulletin, "_cadence_json", lambda c, a: None)
    assert bulletin._cal_line(cfg, NOW) is None


def test_rem_last_done_picks_latest_completion(cfg, monkeypatch):
    monkeypatch.setattr(bulletin, "_cadence_json", lambda c, a: [
        {"title": "old", "completion_date": "2026-07-01T10:00:00+10:00"},
        {"title": "newest", "completion_date": "2026-07-08T09:00:00+10:00"},
        {"title": "mid", "completion_date": "2026-07-05T10:00:00+10:00"},
    ])
    assert bulletin._rem_last_done(cfg) == "newest"


def test_rem_last_done_empty(cfg, monkeypatch):
    monkeypatch.setattr(bulletin, "_cadence_json", lambda c, a: [])
    assert bulletin._rem_last_done(cfg) is None


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

    monkeypatch.setattr(bulletin, "_frontmost_app", lambda: None)
    monkeypatch.setattr(bulletin, "_cal_line", lambda c, n: None)
    monkeypatch.setattr(bulletin, "_rem_last_done", lambda c: None)

    data = bulletin.gather(marrow_conn, cfg, NOW, decision={
        "reasons": [TriggerReason(kind="floor", detail="floor check due")]})
    assert data["wake_parts"] == ["巡回"]
    assert data["budget"]["five_h_pct"] == 40.0
    assert data["budget"]["five_h_reset"] == "14:30"  # 04:30Z -> AEST
    assert data["budget"]["seven_d_pct"] == 12.0
    assert len(data["replay"]) == 1
    assert data["handoff"] is None  # fresh defaults False
    text = bulletin.render(cfg, NOW, data)
    assert text.startswith("Wake: 巡回")


def test_gather_survives_naive_due_at_self_schedule(marrow_conn, cfg, tmp_path, monkeypatch):
    """Live-repro regression: a self_schedule.json entry with an offset-free
    (naive) due_at must not crash gather()/render() end-to-end."""
    make_events_table(marrow_conn)
    marrow_conn.commit()

    monkeypatch.setattr(bulletin, "_frontmost_app", lambda: None)
    monkeypatch.setattr(bulletin, "_cal_line", lambda c, n: None)
    monkeypatch.setattr(bulletin, "_rem_last_done", lambda c: None)

    sp = tmp_path / "ss.json"
    naive_due = (NOW + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
    sp.write_text(json.dumps([{"due_at": naive_due, "intent": "x"}]), encoding="utf-8")
    monkeypatch.setattr(config, "self_schedule_path", lambda c: sp)

    data = bulletin.gather(marrow_conn, cfg, NOW)
    assert data["pending"] == [
        {"hm": (NOW + timedelta(minutes=5)).strftime("%H:%M"), "intent": "x"}
    ]
    text = bulletin.render(cfg, NOW, data)
    assert "Pending self-schedule" in text
