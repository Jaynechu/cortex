from __future__ import annotations

from cortex.collectors.activity import read_activity


def test_read_activity_filters_by_date(marrow_conn):
    marrow_conn.executemany(
        "INSERT INTO ct_activity (ts, sid, channel) VALUES (?, ?, ?)",
        [
            ("2026-07-02T23:00:00+00:00", "sid1", "wx"),
            ("2026-07-03T01:00:00+00:00", "sid2", "wx"),
            ("2026-07-03T02:00:00+00:00", "sid3", "tg"),
        ],
    )
    marrow_conn.commit()

    rows = read_activity(marrow_conn, "2026-07-03")
    assert [r["sid"] for r in rows] == ["sid2", "sid3"]


def test_read_activity_empty_date_returns_empty(marrow_conn):
    rows = read_activity(marrow_conn, "2026-01-01")
    assert rows == []
