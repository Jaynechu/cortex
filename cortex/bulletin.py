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

# Default hard cap, overridable via config [bulletin].max_chars (07-04: the
# forced replay section can push a bulletin past the old 1000-char cap).
MAX_CHARS = 2000

# ct_rate_limit is a flat kv table (key, value, updated_at) a companion
# marrow-side writer owns (parses rate_limit_event off the cortex claude
# stream, see marrow/llm.py). Key contract assumed here (not yet landed at
# write time of this reader) — reconcile against the actual writer once it
# ships: five_hour_pct / five_hour_reset_at, seven_day_pct /
# seven_day_reset_at, window_tokens. Missing table/keys -> honest "no data".
_RATE_LIMIT_KEYS = (
    ("five_hour_pct", "five_hour_reset_at", "5h"),
    ("seven_day_pct", "seven_day_reset_at", "7d"),
)


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))


def _local_hm(ts_iso: str, cfg: dict) -> str:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return dt.astimezone(_tz(cfg)).strftime("%H:%M")


def _bulletin_cfg(cfg: dict) -> dict:
    return cfg.get("bulletin", {}) or {}


def _rate_limit_kv(conn: sqlite3.Connection) -> dict | None:
    """Table missing (writer not landed yet / never enabled) or empty ->
    None, rendered as honest "no data" (DESIGN goal 6)."""
    try:
        rows = conn.execute("SELECT key, value FROM ct_rate_limit").fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    return {row["key"]: row["value"] for row in rows}


def _replay_pairs(conn: sqlite3.Connection, limit_pairs: int) -> list[dict]:
    """Last `limit_pairs` user/assistant conversation pairs, pure DB read —
    excludes tool calls (already stripped by marrow transcript.clean before
    events are archived) and role='tl' self-authored rows. Never relies on
    cortex self-serve queries (Decided 07-04)."""
    if limit_pairs <= 0:
        return []
    try:
        rows = conn.execute(
            "SELECT role, content, timestamp FROM events "
            "WHERE role IN ('user', 'assistant') ORDER BY id DESC LIMIT ?",
            (limit_pairs * 4,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    rows = list(reversed(rows))  # chronological order
    pairs: list[dict] = []
    i = len(rows) - 1
    while i >= 1 and len(pairs) < limit_pairs:
        if rows[i]["role"] == "assistant" and rows[i - 1]["role"] == "user":
            pairs.append({
                "user": rows[i - 1]["content"],
                "assistant": rows[i]["content"],
                "timestamp": rows[i]["timestamp"],
            })
            i -= 2
        else:
            i -= 1
    pairs.reverse()
    return pairs


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

    bcfg = _bulletin_cfg(cfg)
    replay_pairs = _replay_pairs(conn, bcfg.get("replay_pairs", 3))
    rate_limit = _rate_limit_kv(conn)

    return {
        "explanation": explanation,
        "trigger_facts": trigger_facts,
        "last_activity": last_activity,
        "cal_next_3h": list(cal_next_3h or []),
        "usage_top": usage_top,
        "counts": {"events_today": events_today},
        "expect_reply": expect_reply,
        "rate_limit": rate_limit,
        "replay_pairs": replay_pairs,
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

    lines.append(_render_rate_limit(data.get("rate_limit")))

    counts = data.get("counts") or {}
    lines.append(f"Counts: {counts.get('events_today', 0)} events today")

    expect_reply = data.get("expect_reply")
    if expect_reply and expect_reply.get("pending"):
        lines.append(f"Expect-reply: waiting, {expect_reply.get('checks_done', 0)} check(s) done")
    else:
        lines.append("Expect-reply: none pending")

    bcfg = _bulletin_cfg(cfg)
    replay_lines = _render_replay(
        data.get("replay_pairs") or [], bcfg.get("replay_pair_chars", 240)
    )
    if replay_lines:
        lines.extend(replay_lines)

    max_chars = bcfg.get("max_chars", MAX_CHARS)
    text = "\n".join(lines)
    return text[:max_chars]


def _render_rate_limit(kv: dict | None) -> str:
    """Battery gauge line: 5h/7d % + reset time + window size, whichever
    keys are present in the ct_rate_limit snapshot. Missing/empty -> honest
    "no data" (never fabricate a number, DESIGN goal 6)."""
    if not kv:
        return "Budget: no data"
    parts = []
    for pct_key, reset_key, label in _RATE_LIMIT_KEYS:
        raw = kv.get(pct_key)
        if raw is None:
            continue
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            continue
        seg = f"{label} {pct:.0f}%"
        reset = kv.get(reset_key)
        if reset:
            seg += f" (reset {reset})"
        parts.append(seg)
    window = kv.get("window_tokens")
    if window:
        parts.append(f"window {window}")
    return "Budget: " + " · ".join(parts) if parts else "Budget: no data"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def _render_replay(pairs: list[dict], per_pair_chars: int) -> list[str]:
    """Code-forced last-N conversation pairs (Decided 07-04: never rely on
    cortex self-serve recall queries). Empty -> no section at all."""
    if not pairs:
        return []
    lines = [f"Last {len(pairs)} exchange(s):"]
    for pair in pairs:
        lines.append(f"  user: {_truncate(pair.get('user', ''), per_pair_chars)}")
        lines.append(f"  assistant: {_truncate(pair.get('assistant', ''), per_pair_chars)}")
    return lines
