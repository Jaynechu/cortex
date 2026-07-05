"""Feedback ledger writers (C5, collection only — no distill).

Every cortex outbound action lands one 'outbound' row (what/where/why);
her observed reaction lands a 'reaction' row pointing back via outbound_id.
R4 no-respond detection can query outbound rows with no reaction row.
"""
from __future__ import annotations

import json
import sqlite3

from cortex import db


def record_outbound(conn: sqlite3.Connection, channel: str, action: str,
                    content: str | None = None, context: dict | None = None,
                    ts: str | None = None) -> int:
    """Insert an outbound row; returns its id for later reaction linkage.
    action examples: 'message', 'urgent_rem', 'direct_speak'."""
    cur = conn.execute(
        "INSERT INTO ct_feedback (ts, kind, channel, action, content, context)"
        " VALUES (?, 'outbound', ?, ?, ?, ?)",
        (ts or db.utcnow_iso(), channel, action, content,
         json.dumps(context, ensure_ascii=False) if context else None),
    )
    conn.commit()
    return cur.lastrowid


def record_reaction(conn: sqlite3.Connection, outbound_id: int,
                    content: str | None = None, context: dict | None = None,
                    ts: str | None = None) -> int:
    """Insert a reaction row linked to an outbound row."""
    cur = conn.execute(
        "INSERT INTO ct_feedback (ts, kind, content, context, outbound_id)"
        " VALUES (?, 'reaction', ?, ?, ?)",
        (ts or db.utcnow_iso(), content,
         json.dumps(context, ensure_ascii=False) if context else None, outbound_id),
    )
    conn.commit()
    return cur.lastrowid


def unanswered_outbound(conn: sqlite3.Connection, since_ts: str) -> list[sqlite3.Row]:
    """Outbound rows since `since_ts` (UTC ISO) with no linked reaction —
    the query shape R4 no-respond detection reads."""
    return conn.execute(
        "SELECT o.* FROM ct_feedback o"
        " WHERE o.kind = 'outbound' AND o.ts >= ?"
        " AND NOT EXISTS (SELECT 1 FROM ct_feedback r WHERE r.outbound_id = o.id)"
        " ORDER BY o.ts",
        (since_ts,),
    ).fetchall()
