"""Wake bulletin: the short context brief handed to a cortex session on wake.

gather() is a thin DB reader (ct_ tables + events count) that also accepts
optional pass-through facts the caller already holds (pacemaker decision,
pre-fetched calendar window, expect-reply state) and folds them into one
plain data dict. render() is pure — no I/O, no DB — so it can be tested
with synthetic data and reused if the caller assembles its own dict.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_CHARS = 1000


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))


def _local_hm(ts_iso: str, cfg: dict) -> str:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return dt.astimezone(_tz(cfg)).strftime("%H:%M")


def gather(
    conn: sqlite3.Connection,
    cfg: dict,
    now: datetime,
    decision: dict | None = None,
    cal_next_3h: list[dict] | None = None,
    expect_reply_state=None,
) -> dict:
    """Query ct_ tables + events count for `now`'s local date, and fold in
    externally-sourced facts (decision, calendar, expect-reply state) that
    do not live in cortex's own tables. conn must use sqlite3.Row factory."""
    date = now.date().isoformat()

    last_activity = None
    row = conn.execute(
        "SELECT ts, sid, channel FROM ct_activity WHERE ts LIKE ? ORDER BY ts DESC LIMIT 1",
        (f"{date}%",),
    ).fetchone()
    if row is not None:
        last_activity = {"ts": row["ts"], "sid": row["sid"], "channel": row["channel"]}

    usage_top = None
    row = conn.execute(
        "SELECT category, seconds FROM ct_category_usage WHERE date = ? ORDER BY seconds DESC LIMIT 1",
        (date,),
    ).fetchone()
    if row is not None:
        usage_top = {"category": row["category"], "seconds": row["seconds"]}

    events_today = conn.execute(
        "SELECT COUNT(*) FROM events WHERE timestamp LIKE ?", (f"{date}%",)
    ).fetchone()[0]

    explanation = None
    trigger_facts: list[str] = []
    if decision is not None:
        explanation = decision.get("explanation")
        for reason in decision.get("reasons", []):
            detail = getattr(reason, "detail", None)
            if detail is None and isinstance(reason, dict):
                detail = reason.get("detail")
            if detail:
                trigger_facts.append(detail)

    expect_reply = None
    if expect_reply_state is not None and getattr(expect_reply_state, "pending", False):
        expect_reply = {
            "pending": True,
            "checks_done": getattr(expect_reply_state, "checks_done", 0),
        }

    return {
        "explanation": explanation,
        "trigger_facts": trigger_facts,
        "last_activity": last_activity,
        "cal_next_3h": list(cal_next_3h or []),
        "usage_top": usage_top,
        "counts": {"events_today": events_today},
        "expect_reply": expect_reply,
    }


def render(cfg: dict, now: datetime, data: dict) -> str:
    """Pure assembly: data dict -> bulletin text. No DB calls."""
    lines = [f"Now: {now.strftime('%Y-%m-%d %H:%M %a')}"]

    explanation = data.get("explanation")
    trigger_facts = data.get("trigger_facts") or []
    if explanation:
        lines.append(f"Trigger: {explanation}")
    elif trigger_facts:
        lines.append("Trigger: " + "; ".join(trigger_facts))
    else:
        lines.append("Trigger: none")

    last_activity = data.get("last_activity")
    if last_activity:
        hm = _local_hm(last_activity["ts"], cfg)
        sid_short = str(last_activity["sid"])[:8]
        lines.append(f"Last activity: {hm} {last_activity['channel']} (sid {sid_short})")
    else:
        lines.append("Last activity: none today")

    cal = data.get("cal_next_3h") or []
    if cal:
        cal_text = "; ".join(
            f"{c.get('time', '?')} {c.get('title', c.get('summary', '?'))}" for c in cal
        )
        lines.append(f"Calendar (3h): {cal_text}")
    else:
        lines.append("Calendar (3h): none")

    usage_top = data.get("usage_top")
    if usage_top:
        hours = usage_top["seconds"] / 3600
        lines.append(f"Usage today: {usage_top['category']} {hours:.1f}h (top)")
    else:
        lines.append("Usage today: no data")

    counts = data.get("counts") or {}
    lines.append(f"Counts: {counts.get('events_today', 0)} events today")

    expect_reply = data.get("expect_reply")
    if expect_reply and expect_reply.get("pending"):
        lines.append(f"Expect-reply: waiting, {expect_reply.get('checks_done', 0)} check(s) done")
    else:
        lines.append("Expect-reply: none pending")

    text = "\n".join(lines)
    return text[:MAX_CHARS]
