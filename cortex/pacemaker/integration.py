"""Integration layer: real data -> pre-computed numbers -> pure pacemaker.

The pacemaker core stays pure (no DB, no wall clock). This module owns the
tick's own I/O: it assembles the plain-number `context` (activity, token meter,
affect flag, self-schedule queue), runs one tick and appends a wake-log row.
State + occupancy persistence lives in cortex.occupancy (re-exported here while
the tick entry point still calls through this module). Dry-run = log-only.
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from cortex import config
from cortex.occupancy import (  # re-exports: state + occupancy live in occupancy.py
    PacemakerState,
    _finished_window_finals,
    _iso,
    _now,
    _parse_dt,
    _raw_state,
    _today_tokens,
    lie_down,
    load_state,
    log_activation_wake_row,
    parse_due_at,
    save_state,
    store_window_tokens,
    window_tokens_hint,
)
from cortex.pacemaker.core import tick


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
    if isinstance(items, dict):  # tolerate a bare dict (single entry, not wrapped in a list)
        items = [items]
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
    return {
        "active_session": active,
        "last_real_chat_at": last_activity,
        "cal_busy": pm.get("cal_busy_default", False),
        "at_home": pm.get("at_home_default", True),
        "affect_flag": _read_json_file(config.affect_flag_path(cfg), None),
        "self_scheduled": _self_scheduled(cfg),
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
