from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cortex import day_log

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 14, 30, tzinfo=TZ)


def make_events_table(conn):
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, session_id TEXT, "
        "timestamp TEXT, role TEXT, content TEXT, ts_start TEXT, ts_end TEXT)"
    )
    conn.commit()


def test_render_status_shows_last_seen_usage_and_collectors(marrow_conn, base_cfg):
    marrow_conn.execute(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        ("2026-07-03T03:58:00+00:00", "sid1", "wx"),
    )
    marrow_conn.execute(
        "INSERT INTO ct_category_usage (date, category, seconds, updated_at) VALUES (?, ?, ?, ?)",
        ("2026-07-03", "study", 7200, "2026-07-03T04:00:00+00:00"),
    )
    marrow_conn.execute(
        "INSERT INTO ct_collector_log (source, ts, ok, error) VALUES (?, ?, ?, ?)",
        ("knowledgec", "2026-07-03T04:00:00+00:00", 1, None),
    )
    marrow_conn.commit()

    text = day_log.render_status(marrow_conn, base_cfg, NOW)

    assert "13:58 wx" in text
    assert "study 2.0h (top)" in text
    assert "knowledgec: ok" in text


def test_render_status_defaults_when_no_data(marrow_conn, base_cfg):
    text = day_log.render_status(marrow_conn, base_cfg, NOW)
    assert "no activity today" in text
    assert "no usage data" in text
    assert "no runs logged yet" in text


def test_render_today_merges_and_sorts_geofence_and_tl_rows(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    marrow_conn.execute(
        "INSERT INTO ct_geofence (date, time, event, raw_line, source_file, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-07-03", "09:00", "home", "09:00 home", "geo.log", "2026-07-03T00:00:00+00:00"),
    )
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, ts_start, ts_end) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("s1", "2026-07-03T02:05:00Z", "tl", "【专注·3】first",
         "2026-07-03T03:00:00Z", "2026-07-03T03:10:00Z"),
    )
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, ts_start, ts_end) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("s1", "2026-07-02T23:00:00Z", "tl", "not today",
         "2026-07-02T22:00:00Z", "2026-07-02T22:10:00Z"),
    )
    marrow_conn.commit()

    text = day_log.render_today(marrow_conn, base_cfg, NOW)
    lines = text.splitlines()

    assert lines[0] == "## Today"
    assert lines[1] == "09:00 [home]"
    assert lines[2] == "13:00-13:10 【专注·3】first"
    assert "not today" not in text


def test_render_today_empty_placeholder(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    text = day_log.render_today(marrow_conn, base_cfg, NOW)
    assert text == day_log.DEFAULT_TODAY_BODY


def test_render_reminders_placeholder(marrow_conn, base_cfg):
    text = day_log.render_reminders(marrow_conn, base_cfg, NOW)
    assert text == day_log.DEFAULT_REMINDERS_BODY


def test_render_track_placeholder(marrow_conn, base_cfg):
    text = day_log.render_track(marrow_conn, base_cfg, NOW)
    assert text == day_log.DEFAULT_TRACK_BODY


def test_update_preserves_first_and_notes_across_rerender(tmp_path, marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    path = tmp_path / "day_log.md"
    day_log.update(path, marrow_conn, base_cfg, NOW)
    text = path.read_text()
    assert text.splitlines()[0] == "2026-07-03"

    # cortex writes First, her writes Notes
    first_idx = text.index(day_log.FIRST_START)
    first_end_idx = text.index(day_log.FIRST_END)
    text = (
        text[: first_idx + len(day_log.FIRST_START) + 1]
        + "## First\ncoaxed her out of bed\n"
        + text[first_end_idx:]
    )
    notes_marker_idx = text.index(day_log.NOTES_START)
    her_note = text[: notes_marker_idx + len(day_log.NOTES_START) + 1] + "## Stellan's Notes\nremember to buy milk\n"
    path.write_text(her_note)

    marrow_conn.execute(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        ("2026-07-03T05:00:00+00:00", "sid1", "wx"),
    )
    marrow_conn.commit()

    later = NOW + timedelta(hours=1)
    day_log.update(path, marrow_conn, base_cfg, later)
    text2 = path.read_text()

    assert "coaxed her out of bed" in text2
    assert "remember to buy milk" in text2
    assert "15:00 wx" in text2


def test_render_day_log_zone_markers_present(marrow_conn, base_cfg):
    make_events_table(marrow_conn)
    text = day_log.render_day_log(marrow_conn, base_cfg, NOW)
    for marker in (
        day_log.FIRST_START,
        day_log.FIRST_END,
        day_log.STATUS_START,
        day_log.STATUS_END,
        day_log.TODAY_START,
        day_log.TODAY_END,
        day_log.REMINDERS_START,
        day_log.REMINDERS_END,
        day_log.TRACK_START,
        day_log.TRACK_END,
        day_log.NOTES_START,
    ):
        assert marker in text


def test_new_day_creates_fresh_file(tmp_path):
    path = tmp_path / "day_log.md"
    day_log.new_day(path, "2026-07-04")
    text = path.read_text()
    assert text.splitlines()[0] == "2026-07-04"
    assert day_log.NOTES_START in text
    assert "## Stellan's Notes" in text
    assert "## First" in text


def test_archive_moves_file_named_by_l1_date(tmp_path):
    path = tmp_path / "day_log.md"
    day_log.new_day(path, "2026-07-03")
    archive_dir = tmp_path / "archive"

    dest = day_log.archive(path, archive_dir)

    assert not path.exists()
    assert dest == archive_dir / "2026-07-03.md"
    assert dest.exists()
    assert dest.read_text().splitlines()[0] == "2026-07-03"


def test_archive_missing_file_raises(tmp_path):
    path = tmp_path / "does_not_exist.md"
    try:
        day_log.archive(path, tmp_path / "archive")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass
