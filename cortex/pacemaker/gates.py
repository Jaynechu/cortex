"""Gate chain: each gate is a pure function (state, context, config, now)
-> GateResult. A wake is allowed only if every gate allows.

Expected config shape (config["gates"]):
    {
        "cooldown_min_min": float,       # cooldown draw lower bound (minutes)
        "cooldown_max_min": float,       # cooldown draw upper bound (minutes)
        "wake_stale_min": float,         # wake-in-progress presumed dead after this
        "daily_message_cap": int,        # max wakes-that-messaged per day
        "night": {"start": "00:00", "end": "06:00", "cap": 1},
        "token_budget_min_reserve": float,  # min remaining budget fraction (0-1)
    }

Expected context keys used here:
    "messages_sent_today": int           # integration-counted, from wake log
    "token_budget_remaining_fraction": float  # integration-computed from audit_log
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


def gate_cooldown(state, context: dict, config: dict, now: datetime) -> GateResult:
    """Post-wake cooldown, clocked from lie-down (cooldown_until drawn there).
    Also blocks while a wake is in progress (woke, not yet lain down), with a
    staleness escape so a crashed wake can't wedge the pacemaker forever."""
    gates_cfg = config.get("gates", {})
    last_wake_at = getattr(state, "last_wake_at", None)
    last_lie_down_at = getattr(state, "last_lie_down_at", None)
    if last_wake_at is not None and (last_lie_down_at is None or last_lie_down_at < last_wake_at):
        elapsed_min = (now - last_wake_at).total_seconds() / 60.0
        stale_min = gates_cfg.get("wake_stale_min", 30)
        if elapsed_min < stale_min:
            return GateResult("cooldown", False, f"wake in progress ({elapsed_min:.1f}min)")

    cooldown_until = getattr(state, "cooldown_until", None)
    if cooldown_until is not None and now < cooldown_until:
        remaining_min = (cooldown_until - now).total_seconds() / 60.0
        return GateResult("cooldown", False, f"cooling down, {remaining_min:.1f}min left")
    return GateResult("cooldown", True, "no active cooldown")


def gate_daily_cap(state, context: dict, config: dict, now: datetime) -> GateResult:
    cap = config.get("gates", {}).get("daily_message_cap")
    if cap is None:
        return GateResult("daily-cap", True, "no cap configured")

    sent_today = context.get("messages_sent_today", 0)
    if sent_today >= cap:
        return GateResult("daily-cap", False, f"{sent_today}/{cap} messages sent today")
    return GateResult("daily-cap", True, f"{sent_today}/{cap} messages sent today")


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


def gate_token_budget(state, context: dict, config: dict, now: datetime) -> GateResult:
    min_reserve = config.get("gates", {}).get("token_budget_min_reserve")
    if min_reserve is None:
        return GateResult("token-budget", True, "no budget floor configured")

    remaining = context.get("token_budget_remaining_fraction", 1.0)
    if remaining < min_reserve:
        return GateResult(
            "token-budget", False, f"{remaining:.2f} remaining < {min_reserve:.2f} floor"
        )
    return GateResult("token-budget", True, f"{remaining:.2f} remaining")


ALL_GATES = (
    gate_cooldown,
    gate_daily_cap,
    gate_night_mode,
    gate_token_budget,
)


def run_gates(state, context: dict, config: dict, now: datetime) -> list[GateResult]:
    """Run every gate (no short-circuit) so all results are available for logging."""
    return [gate(state, context, config, now) for gate in ALL_GATES]
