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

def _state_to_json(state: PacemakerState, base: dict | None = None) -> str:
    obj = dict(base or {})  # preserve side-channel keys (window_tokens, schedule_fired)
    # Drop any legacy desire/expect_reply/cortex_session_date keys carried in
    # from an old row (cortex_session_date: rebirth retired, 3155246).
    obj.pop("desire", None)
    obj.pop("expect_reply", None)
    obj.pop("cortex_session_date", None)
    obj.update({
        "next_floor_due_at": _iso(state.next_floor_due_at),
        "last_wake_at": _iso(state.last_wake_at),
        "last_lie_down_at": _iso(state.last_lie_down_at),
        "night_cap_key": state.night_cap_key,
        "night_wake_count": state.night_wake_count,
        "cortex_session_id": state.cortex_session_id,
    })
    return json.dumps(obj)


def _state_from_json(text: str) -> PacemakerState:
    # Tolerant load: legacy rows may still carry desire/expect_reply/
    # cortex_session_date keys — they are simply ignored (retired engines).
    o = json.loads(text)
    return PacemakerState(
        next_floor_due_at=_parse_dt(o.get("next_floor_due_at")),
        last_wake_at=_parse_dt(o.get("last_wake_at")),
        last_lie_down_at=_parse_dt(o.get("last_lie_down_at")),
        night_cap_key=o.get("night_cap_key"),
        night_wake_count=o.get("night_wake_count", 0),
        cortex_session_id=o.get("cortex_session_id"),
    )


def load_state(conn: sqlite3.Connection) -> PacemakerState:
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    return _state_from_json(row["state"]) if row else PacemakerState()


def store_window_tokens(conn: sqlite3.Connection, tokens: int | None) -> None:
    """Stash the live window-token count on the ct_pacemaker_state JSON so the
    wakeup note's Budget line can read it (note._window_tokens). Merged into
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


def _raw_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
    try:
        return json.loads(row["state"]) if row else {}
    except (ValueError, TypeError):
        return {}


def load_schedule_fired(conn: sqlite3.Connection) -> dict:
    """{duty name: last-fired local date} — kept on the raw state JSON (not the
    pure PacemakerState) so it survives tick saves independently."""
    val = _raw_state(conn).get("schedule_fired")
    return dict(val) if isinstance(val, dict) else {}


def mark_schedule_fired(conn: sqlite3.Connection, name: str, date: str) -> None:
    obj = _raw_state(conn)
    fired = obj.get("schedule_fired")
    fired = dict(fired) if isinstance(fired, dict) else {}
    fired[name] = date
    obj["schedule_fired"] = fired
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
        (json.dumps(obj), db.utcnow_iso()),
    )
    conn.commit()


def save_state(conn: sqlite3.Connection, state: PacemakerState) -> None:
    base = _raw_state(conn)  # keep side-channel keys (window_tokens, schedule_fired)
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
        (_state_to_json(state, base), db.utcnow_iso()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# context builders (real data -> plain numbers)
# --------------------------------------------------------------------------

def _latest_activity_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(ts) AS ts FROM ct_activity").fetchone()
    return _parse_dt(row["ts"]) if row and row["ts"] else None


def _today_tokens(conn: sqlite3.Connection, now: datetime) -> int:
    """SUM(COALESCE(net_tokens, tokens)) for `now`'s local date — the daily
    budget gate counts NET spend (cache-miss rewrite + output); a pre-migration
    row with no net_tokens degrades to its total `tokens`. ts is stored UTC ISO;
    filter from local midnight (converted to UTC) then confirm the local date so
    the gate resets naturally at local midnight."""
    tz = now.tzinfo
    start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        ZoneInfo("UTC")).isoformat()
    try:
        rows = conn.execute(
            "SELECT ts, COALESCE(net_tokens, tokens) AS spend FROM ct_wake_log "
            "WHERE tokens IS NOT NULL AND ts >= ?",
            (start_utc,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    total = 0
    today = now.date()
    for row in rows:
        try:
            if _parse_dt(row["ts"]).astimezone(tz).date() == today:
                total += int(row["spend"])
        except (TypeError, ValueError, AttributeError):
            continue
    return total


def _read_json_file(path, default):
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    return default


def _self_scheduled(cfg: dict) -> list[dict]:
    items = _read_json_file(config.self_schedule_path(cfg), [])
    if isinstance(items, dict):  # tolerate a bare dict (single entry, not wrapped in a list)
        items = [items]
    tz = ZoneInfo(cfg["core"]["timezone"])
    out = []
    for item in items if isinstance(items, list) else []:
        due = parse_due_at(item.get("due_at"), tz) if isinstance(item, dict) else None
        if due is not None:
            out.append({**item, "due_at": due})
    return out


def _schedule_due(conn: sqlite3.Connection, cfg: dict, now: datetime) -> list[dict]:
    from cortex.pacemaker import schedule as schedule_mod

    entries = cfg.get("schedule", []) or []
    fired = load_schedule_fired(conn)
    return schedule_mod.due_duties(entries, now, fired)


def build_context(conn: sqlite3.Connection, cfg: dict, now: datetime, state: PacemakerState) -> dict:
    pm = cfg["pacemaker"]
    last_activity = _latest_activity_at(conn)
    active = False
    if last_activity is not None:
        active = (now - last_activity).total_seconds() / 60.0 <= pm.get("active_window_min", 5)
    return {
        "active_session": active,
        "last_real_chat_at": last_activity,
        "cal_busy": pm.get("cal_busy_default", False),
        "at_home": pm.get("at_home_default", True),
        "affect_flag": _read_json_file(config.affect_flag_path(cfg), None),
        "self_scheduled": _self_scheduled(cfg),
        "schedule": _schedule_due(conn, cfg, now),
        "today_tokens": _today_tokens(conn, now),
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
             rng: random.Random | None = None, minutes: float | None = None) -> None:
    """Mark wake end (C-wm): lie_down chooses the next internal wake. `minutes`
    = an explicit choice (clamped to [floor_min_min, floor_max_min]); None =
    a uniform "dice" draw within that window (preserves prior behaviour). The
    clock restarts from lie-down. Called by the tick entry point after a wake
    finishes — including on wake failure, so a crashed wake can't wedge it."""
    from cortex.pacemaker.triggers import reschedule_floor

    now = now or _now(cfg)
    rng = rng or random.Random()

    state = load_state(conn)
    new_state = dataclasses.replace(
        state,
        next_floor_due_at=reschedule_floor(now, cfg, rng, minutes),
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
