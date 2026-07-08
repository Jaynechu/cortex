"""Integration layer: real data -> pre-computed numbers -> pure pacemaker.

The pacemaker core stays pure (no DB, no wall clock). This module owns all
I/O: it queries marrow audit_log (token meter) + cortex ct_ tables (activity,
wake log), reads the affect-flag file and self-schedule queue, assembles the
plain-number `context`, resumes persisted `PacemakerState`, runs one tick, then
persists the new state and appends a wake-log row. Dry-run = log-only: no
outbound exists yet (C5), so a wake decision is only recorded.
"""
from __future__ import annotations

import dataclasses
import json
import random
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from cortex import config, db
from cortex.pacemaker.core import PacemakerState, tick
from cortex.pacemaker.desire import DesireState
from cortex.pacemaker.expect_reply import ExpectReplyState


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


def parse_due_at(value: str | None, tz: ZoneInfo) -> datetime | None:
    """Parse a self-schedule due_at. Accepts tz-aware ISO and offset-free (naive)
    ISO; naive is interpreted as local wall time in `tz` (DST-correct). The
    convention is offset-free local — no hardcoded UTC offset (breaks under DST)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


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
        "last_lie_down_at": _iso(state.last_lie_down_at),
        "night_cap_key": state.night_cap_key,
        "night_wake_count": state.night_wake_count,
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
        last_lie_down_at=_parse_dt(o.get("last_lie_down_at")),
        night_cap_key=o.get("night_cap_key"),
        night_wake_count=o.get("night_wake_count", 0),
        cortex_session_id=o.get("cortex_session_id"),
        cortex_session_date=o.get("cortex_session_date"),
    )


def load_state(conn: sqlite3.Connection) -> PacemakerState:
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    return _state_from_json(row["state"]) if row else PacemakerState()


def store_window_tokens(conn: sqlite3.Connection, tokens: int | None) -> None:
    """Stash the live window-token count on the ct_pacemaker_state JSON so the
    wakeup note's Budget line can read it (bulletin._window_tokens). Merged into
    the raw JSON (not the dataclass) so it survives independently of tick saves."""
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    try:
        obj = json.loads(row["state"]) if row else {}
    except (ValueError, TypeError):
        obj = {}
    obj["window_tokens"] = int(tokens) if tokens else None
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
        (json.dumps(obj), db.utcnow_iso()),
    )
    conn.commit()


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

def _latest_activity_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(ts) AS ts FROM ct_activity").fetchone()
    return _parse_dt(row["ts"]) if row and row["ts"] else None


def _read_json_file(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return default


def _self_scheduled(cfg: dict) -> list[dict]:
    items = _read_json_file(config.self_schedule_path(cfg), [])
    tz = ZoneInfo(cfg["core"]["timezone"])
    out = []
    for item in items if isinstance(items, list) else []:
        due = parse_due_at(item.get("due_at"), tz) if isinstance(item, dict) else None
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


def lie_down(conn: sqlite3.Connection, cfg: dict, now: datetime | None = None,
             rng: random.Random | None = None) -> None:
    """Mark wake end (C-wm): the floor clock restarts from lie-down (uniform
    10-55min draw). Called by the tick entry point after a wake finishes —
    including on wake failure, so a crashed wake can't wedge the floor."""
    from cortex.pacemaker.triggers import reschedule_floor

    now = now or _now(cfg)
    rng = rng or random.Random()

    state = load_state(conn)
    new_state = dataclasses.replace(
        state,
        next_floor_due_at=reschedule_floor(now, cfg, rng),
        last_lie_down_at=now,
    )
    save_state(conn, new_state)


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
