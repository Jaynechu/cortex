"""Gate chain: each gate is a pure function (state, context, config, now)
-> GateResult. A wake is allowed only if every gate allows. Night mode is the
sole gate — spend protection lives in the 150k per-wake fuse + bulletin battery
gauge, not here.

Expected config shape (config["gates"]):
    {
        "night": {"start": "00:00", "end": "06:00", "cap": 1},
    }

Expected context keys used here:
    "trigger_kinds": list[str]           # fired TriggerReason kinds, set by core.tick
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

# Trigger kinds that pierce night mode (C-wm: schedule + rules pierce; only
# desire/floor/expect_reply consume the nightly cap).
PIERCE_KINDS = frozenset({"event", "affect_flag", "self_scheduled"})


@dataclass(frozen=True)
class GateResult:
    name: str
    allowed: bool
    reason: str


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def _in_window(now_time: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_time < end
    # wraps midnight, e.g. 23:30 -> 07:00
    return now_time >= start or now_time < end


def _night_cfg(config: dict) -> dict:
    return config.get("gates", {}).get("night", {}) or {}


def night_key(config: dict, now: datetime) -> str | None:
    """Identity of the night window `now` falls in (date the window started),
    or None outside the window. core.tick uses this to reset/advance the
    nightly wake counter."""
    cfg = _night_cfg(config)
    start = _parse_hhmm(cfg.get("start", "00:00"))
    end = _parse_hhmm(cfg.get("end", "06:00"))
    now_time = now.timetz().replace(tzinfo=None)
    if not _in_window(now_time, start, end):
        return None
    if start > end and now_time < end:  # wrapped window, past midnight
        return (now - timedelta(days=1)).date().isoformat()
    return now.date().isoformat()


def gate_night_mode(state, context: dict, config: dict, now: datetime) -> GateResult:
    """Night window (replaces fatigue gate): desire/floor/expect_reply wakes
    capped per night; self_scheduled and rule-type triggers pierce."""
    key = night_key(config, now)
    if key is None:
        return GateResult("night-mode", True, "outside night window")

    kinds = set(context.get("trigger_kinds", []))
    if kinds & PIERCE_KINDS:
        return GateResult("night-mode", True, "pierced by schedule/rule trigger")

    cap = _night_cfg(config).get("cap", 1)
    count = getattr(state, "night_wake_count", 0)
    if getattr(state, "night_cap_key", None) != key:
        count = 0  # counter belongs to a previous night
    if count >= cap:
        return GateResult("night-mode", False, f"night cap reached ({count}/{cap})")
    return GateResult("night-mode", True, f"night wake {count}/{cap} used")


ALL_GATES = (
    gate_night_mode,
)


def run_gates(state, context: dict, config: dict, now: datetime) -> list[GateResult]:
    """Run every gate (no short-circuit) so all results are available for logging."""
    return [gate(state, context, config, now) for gate in ALL_GATES]
