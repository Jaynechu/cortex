"""Integration layer: real data -> pre-computed numbers -> pure pacemaker.

The pacemaker core stays pure (no DB, no wall clock). This module owns all
I/O: it queries marrow audit_log (token meter) + cortex ct_ tables (activity,
wake log), reads the affect-flag file and self-schedule queue, assembles the
plain-number `context`, resumes persisted `PacemakerState`, runs one tick, then
persists the new state and appends a wake-log row. Dry-run = log-only: no
outbound exists yet (C5), so a wake decision is only recorded.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cortex import config, db
from cortex.pacemaker.core import PacemakerState, tick
from cortex.pacemaker.desire import DesireState
from cortex.pacemaker.expect_reply import ExpectReplyState

_USAGE_RE = re.compile(r"in=(\d+) out=(\d+) cache_read=(\d+) cache_write=(\d+)")


# --------------------------------------------------------------------------
# datetime helpers
# --------------------------------------------------------------------------

def _now(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


# --------------------------------------------------------------------------
# state persistence (ct_pacemaker_state, single row id=1)
# --------------------------------------------------------------------------

def _state_to_json(state: PacemakerState) -> str:
    d = state.desire
    er = state.expect_reply
    return json.dumps({
        "desire": {
            "attachment": d.attachment, "curiosity": d.curiosity,
            "worry": d.worry, "duty": d.duty, "last_tick_at": _iso(d.last_tick_at),
        },
        "expect_reply": {
            "pending": er.pending, "sent_at": _iso(er.sent_at),
            "last_check_at": _iso(er.last_check_at),
            "checks_done": er.checks_done, "tone_level": er.tone_level,
        },
        "next_floor_due_at": _iso(state.next_floor_due_at),
        "last_wake_at": _iso(state.last_wake_at),
        "cortex_session_id": state.cortex_session_id,
        "cortex_session_date": state.cortex_session_date,
    })


def _state_from_json(text: str) -> PacemakerState:
    o = json.loads(text)
    d = o.get("desire", {})
    er = o.get("expect_reply", {})
    return PacemakerState(
        desire=DesireState(
            attachment=d.get("attachment", 0.0), curiosity=d.get("curiosity", 0.0),
            worry=d.get("worry", 0.0), duty=d.get("duty", 0.0),
            last_tick_at=_parse_dt(d.get("last_tick_at")),
        ),
        expect_reply=ExpectReplyState(
            pending=er.get("pending", False), sent_at=_parse_dt(er.get("sent_at")),
            last_check_at=_parse_dt(er.get("last_check_at")),
            checks_done=er.get("checks_done", 0), tone_level=er.get("tone_level", 0),
        ),
        next_floor_due_at=_parse_dt(o.get("next_floor_due_at")),
        last_wake_at=_parse_dt(o.get("last_wake_at")),
        cortex_session_id=o.get("cortex_session_id"),
        cortex_session_date=o.get("cortex_session_date"),
    )


def load_state(conn: sqlite3.Connection) -> PacemakerState:
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    return _state_from_json(row["state"]) if row else PacemakerState()


def save_state(conn: sqlite3.Connection, state: PacemakerState) -> None:
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
        (_state_to_json(state), db.utcnow_iso()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# context builders (real data -> plain numbers)
# --------------------------------------------------------------------------

def token_budget_remaining_fraction(conn: sqlite3.Connection, cfg: dict, now: datetime) -> float:
    """Fraction of the rolling-window token budget still available. Tolerant:
    missing audit_log or zero budget -> 1.0 (gate becomes a no-op)."""
    meter = cfg["pacemaker"]["token_meter"]
    budget = meter.get("daily_budget_tokens", 0) or 0
    if budget <= 0:
        return 1.0
    window_hours = meter.get("window_hours", 24)
    since = (now - timedelta(hours=window_hours)).astimezone(ZoneInfo("UTC"))
    try:
        rows = conn.execute(
            "SELECT summary FROM audit_log WHERE action='llm_call_cost' AND occurred_at >= ?",
            (since.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchall()
    except sqlite3.OperationalError:
        return 1.0
    used = 0
    for row in rows:
        m = _USAGE_RE.search(row["summary"] or "")
        if m:
            used += sum(int(g) for g in m.groups())
    return max(0.0, 1.0 - used / budget)


def _latest_activity_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(ts) AS ts FROM ct_activity").fetchone()
    return _parse_dt(row["ts"]) if row and row["ts"] else None


def _wakes_today(conn: sqlite3.Connection, now: datetime) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(ZoneInfo("UTC"))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM ct_wake_log WHERE wake=1 AND ts >= ?",
        (start.isoformat(),),
    ).fetchone()
    return row["n"] if row else 0


def _read_json_file(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return default


def _self_scheduled(cfg: dict) -> list[dict]:
    items = _read_json_file(config.self_schedule_path(cfg), [])
    out = []
    for item in items if isinstance(items, list) else []:
        due = _parse_dt(item.get("due_at")) if isinstance(item, dict) else None
        if due is not None:
            out.append({**item, "due_at": due})
    return out


def build_context(conn: sqlite3.Connection, cfg: dict, now: datetime, state: PacemakerState) -> dict:
    pm = cfg["pacemaker"]
    last_activity = _latest_activity_at(conn)
    active = False
    if last_activity is not None:
        active = (now - last_activity).total_seconds() / 60.0 <= pm.get("active_window_min", 5)
    replied = False
    if state.expect_reply.pending and state.expect_reply.sent_at and last_activity:
        replied = last_activity > state.expect_reply.sent_at
    return {
        "active_session": active,
        "last_real_chat_at": last_activity,
        "replied": replied,
        "messages_sent_today": _wakes_today(conn, now),
        "token_budget_remaining_fraction": token_budget_remaining_fraction(conn, cfg, now),
        "cal_busy": pm.get("cal_busy_default", False),
        "at_home": pm.get("at_home_default", True),
        "affect_flag": _read_json_file(config.affect_flag_path(cfg), None),
        "self_scheduled": _self_scheduled(cfg),
        "events": [],
    }


# --------------------------------------------------------------------------
# wake log + tick orchestration
# --------------------------------------------------------------------------

def write_wake_log(conn: sqlite3.Connection, decision: dict, now: datetime, dry_run: bool) -> None:
    reasons = "; ".join(r.detail for r in decision["reasons"]) or None
    gated = ", ".join(g.name for g in decision["gated_by"]) or None
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons, gated_by, explanation)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (now.astimezone(ZoneInfo("UTC")).isoformat(), 1 if decision["wake"] else 0,
         1 if dry_run else 0, reasons, gated, decision["explanation"]),
    )
    conn.commit()


def run_tick(conn: sqlite3.Connection, cfg: dict, now: datetime | None = None,
             rng: random.Random | None = None) -> dict:
    """One pacemaker tick against live data. Persists state + wake log, returns
    the decision. Log-only: never triggers outbound (none exists in v1)."""
    now = now or _now(cfg)
    rng = rng or random.Random()
    dry_run = bool(cfg["pacemaker"].get("dry_run", True))

    state = load_state(conn)
    context = build_context(conn, cfg, now, state)
    decision, new_state = tick(state, context, cfg, now, rng)

    save_state(conn, new_state)
    write_wake_log(conn, decision, now, dry_run)
    return decision
