from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cortex import bulletin
from cortex.pacemaker.expect_reply import ExpectReplyState
from cortex.pacemaker.triggers import TriggerReason

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 14, 30, tzinfo=TZ)


def make_events_table(conn):
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, session_id TEXT, "
        "timestamp TEXT, role TEXT, content TEXT)"
    )
    conn.commit()


def test_render_sections_present_and_under_1000_chars(base_cfg):
    data = {
        "explanation": "14:30 wake: desire.attachment 0.85>=0.80",
        "trigger_facts": [],
        "last_activity": {"ts": "2026-07-03T03:58:00+00:00", "sid": "abc12345-def", "channel": "wx"},
        "cal_next_3h": [{"time": "15:00", "title": "Grand round"}],
        "usage_top": {"category": "study", "seconds": 11520},
        "counts": {"events_today": 12},
        "expect_reply": {"pending": True, "checks_done": 1},
    }
    text = bulletin.render(base_cfg, NOW, data)

    assert len(text) < 1000
    for label in (
        "Now:",
        "Trigger:",
        "Last activity:",
        "Calendar (3h):",
        "Usage today:",
        "Counts:",
        "Expect-reply:",
    ):
        assert label in text

    assert "Grand round" in text
    assert "study" in text
    assert "12 events today" in text
    assert "waiting" in text


def test_render_empty_data_falls_back_to_none_placeholders(base_cfg):
    text = bulletin.render(base_cfg, NOW, {})
    assert "Trigger: none" in text
    assert "Last activity: none today" in text
    assert "Calendar (3h): none" in text
    assert "Usage today: no data" in text
    assert "Counts: 0 events today" in text
    assert "Expect-reply: none pending" in text


def test_gather_reads_ct_tables_and_events_count(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
        [
            ("s1", "2026-07-03T00:10:00Z", "user", "hi"),
            ("s1", "2026-07-03T00:11:00Z", "assistant", "hello"),
            ("s1", "2026-07-02T23:00:00Z", "user", "yesterday"),
        ],
    )
    marrow_conn.execute(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        ("2026-07-03T03:58:00+00:00", "sid-today", "wx"),
    )
    marrow_conn.execute(
        "INSERT INTO ct_category_usage (date, category, seconds, updated_at) VALUES (?, ?, ?, ?)",
        ("2026-07-03", "study", 3600, "2026-07-03T04:00:00+00:00"),
    )
    marrow_conn.commit()

    data = bulletin.gather(marrow_conn, base_cfg, NOW)

    assert data["counts"]["events_today"] == 2
    assert data["last_activity"]["sid"] == "sid-today"
    assert data["usage_top"] == {"category": "study", "seconds": 3600}
    assert data["expect_reply"] is None


def test_gather_folds_in_decision_and_calendar_and_expect_reply(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    decision = {
        "explanation": "14:30 wake: floor check due",
        "reasons": [TriggerReason(kind="floor", detail="floor check due")],
    }
    cal = [{"time": "16:00", "title": "Gym"}]
    er_state = ExpectReplyState(pending=True, sent_at=NOW, last_check_at=NOW, checks_done=2)

    data = bulletin.gather(
        marrow_conn, base_cfg, NOW, decision=decision, cal_next_3h=cal, expect_reply_state=er_state
    )

    assert data["explanation"] == "14:30 wake: floor check due"
    assert data["trigger_facts"] == ["floor check due"]
    assert data["cal_next_3h"] == cal
    assert data["expect_reply"] == {"pending": True, "checks_done": 2}


def test_render_truncates_to_max_chars(base_cfg):
    long_cal = [{"time": "15:00", "title": "x" * 2000}]
    text = bulletin.render(base_cfg, NOW, {"cal_next_3h": long_cal})
    assert len(text) == bulletin.MAX_CHARS


def test_render_budget_no_data_when_rate_limit_missing(base_cfg):
    text = bulletin.render(base_cfg, NOW, {})
    assert "Budget: no data" in text


def test_render_budget_line_from_rate_limit_kv(base_cfg):
    data = {
        "rate_limit": {
            "five_hour_pct": "42",
            "five_hour_reset_at": "14:30",
            "seven_day_pct": "15",
            "window_tokens": "26000",
        }
    }
    text = bulletin.render(base_cfg, NOW, data)
    assert "Budget: 5h 42% (reset 14:30) · 7d 15% · window 26000" in text


def test_gather_rate_limit_missing_table_is_none(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    data = bulletin.gather(marrow_conn, base_cfg, NOW)
    assert data["rate_limit"] is None


def test_gather_rate_limit_reads_kv_table(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    marrow_conn.execute(
        "CREATE TABLE ct_rate_limit (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
    )
    marrow_conn.execute(
        "INSERT INTO ct_rate_limit (key, value, updated_at) VALUES ('five_hour_pct', '42', ?)",
        ("2026-07-03T00:00:00Z",),
    )
    marrow_conn.commit()

    data = bulletin.gather(marrow_conn, base_cfg, NOW)
    assert data["rate_limit"]["five_hour_pct"] == "42"


def test_render_replay_absent_when_no_pairs(base_cfg):
    text = bulletin.render(base_cfg, NOW, {"replay_pairs": []})
    assert "exchange" not in text


def test_render_replay_shows_last_pairs_truncated(base_cfg):
    pairs = [
        {"user": "hi", "assistant": "hello"},
        {"user": "y" * 500, "assistant": "z" * 500},
    ]
    text = bulletin.render(base_cfg, NOW, {"replay_pairs": pairs})
    assert "Last 2 exchange(s):" in text
    assert "user: hi" in text
    assert "assistant: hello" in text
    # per-pair truncation keeps a single message well under its raw length
    assert "y" * 500 not in text


def test_gather_replay_pairs_excludes_tl_and_tool_rows(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
        [
            ("s1", "2026-07-03T00:01:00Z", "user", "first question"),
            ("s1", "2026-07-03T00:02:00Z", "assistant", "first answer"),
            ("s1", "2026-07-03T00:03:00Z", "tl", "【专注·3】not a pair"),
            ("s1", "2026-07-03T00:04:00Z", "user", "second question"),
            ("s1", "2026-07-03T00:05:00Z", "assistant", "second answer"),
        ],
    )
    marrow_conn.commit()

    data = bulletin.gather(marrow_conn, base_cfg, NOW)
    pairs = data["replay_pairs"]

    assert len(pairs) == 2
    assert pairs[-1] == {
        "user": "second question",
        "assistant": "second answer",
        "timestamp": "2026-07-03T00:05:00Z",
    }
    assert all("not a pair" not in p["user"] and "not a pair" not in p["assistant"] for p in pairs)


def test_gather_replay_pairs_respects_config_limit(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    marrow_conn.executemany(
        "INSERT INTO events (session_id, timestamp, role, content) VALUES (?, ?, ?, ?)",
        [
            ("s1", f"2026-07-03T00:{i:02d}:00Z", role, f"msg{i}")
            for i, role in enumerate(["user", "assistant"] * 5)
        ],
    )
    marrow_conn.commit()
    cfg = dict(base_cfg)
    cfg["bulletin"] = {"replay_pairs": 1}

    data = bulletin.gather(marrow_conn, cfg, NOW)
    assert len(data["replay_pairs"]) == 1
