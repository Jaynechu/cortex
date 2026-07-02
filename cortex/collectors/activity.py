"""ct_activity reader helper.

Not a collector: the ct_activity table is written by A1's marrow Stop
hook (per-turn ts/sid/channel), a separate work block. This module only
provides the schema-shaped read helper cortex consumers need; it is not
registered in collectors.COLLECTORS/run_all.
"""
from __future__ import annotations

import sqlite3


def read_activity(conn: sqlite3.Connection, date: str) -> list[sqlite3.Row]:
    """Rows for a local calendar date (YYYY-MM-DD), oldest first.

    ts is stored as UTC ISO-8601; callers needing local-day filtering must
    pass a date already resolved in their own timezone and this compares
    on the UTC date prefix, which is a caller-level UNVERIFIED simplification
    until A1 lands and real ts values exist to check against local-day
    boundaries.
    """
    cur = conn.execute(
        "SELECT ts, sid, channel FROM ct_activity WHERE ts LIKE ? ORDER BY ts ASC",
        (f"{date}%",),
    )
    return cur.fetchall()
