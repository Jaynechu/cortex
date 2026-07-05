from __future__ import annotations

from cortex import db, feedback


def test_record_outbound_and_reaction_links_by_id(marrow_conn):
    outbound_id = feedback.record_outbound(
        marrow_conn, "wechat", "message", content="hi", context={"k": 1}
    )
    assert outbound_id is not None

    reaction_id = feedback.record_reaction(marrow_conn, outbound_id, content="thanks")
    assert reaction_id is not None

    outbound_row = marrow_conn.execute(
        "SELECT * FROM ct_feedback WHERE id = ?", (outbound_id,)
    ).fetchone()
    assert outbound_row["kind"] == "outbound"
    assert outbound_row["channel"] == "wechat"
    assert outbound_row["action"] == "message"
    assert outbound_row["content"] == "hi"

    reaction_row = marrow_conn.execute(
        "SELECT * FROM ct_feedback WHERE id = ?", (reaction_id,)
    ).fetchone()
    assert reaction_row["kind"] == "reaction"
    assert reaction_row["outbound_id"] == outbound_id
    assert reaction_row["content"] == "thanks"


def test_unanswered_outbound_returns_only_rows_without_reaction(marrow_conn):
    since = db.utcnow_iso()
    answered = feedback.record_outbound(marrow_conn, "wechat", "message")
    unanswered = feedback.record_outbound(marrow_conn, "wechat", "message")
    feedback.record_reaction(marrow_conn, answered)

    rows = feedback.unanswered_outbound(marrow_conn, since)
    ids = {row["id"] for row in rows}

    assert unanswered in ids
    assert answered not in ids


def test_unanswered_outbound_respects_since_ts(marrow_conn):
    old = feedback.record_outbound(marrow_conn, "wechat", "message", ts="2020-01-01T00:00:00+00:00")
    since = db.utcnow_iso()
    recent = feedback.record_outbound(marrow_conn, "wechat", "message")

    rows = feedback.unanswered_outbound(marrow_conn, since)
    ids = {row["id"] for row in rows}

    assert recent in ids
    assert old not in ids
