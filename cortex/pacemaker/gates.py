"""Gate chain: each gate is a pure function (state, context, config, now)
-> GateResult. A wake is allowed only if every gate allows.

Expected config shape (config["gates"]):
    {
        "cooldown_min": float,           # min minutes since last wake
        "daily_message_cap": int,        # max wakes-that-messaged per day
        "fatigue_windows": [             # local time-of-day windows, wraps midnight
            {"start": "23:30", "end": "07:00"},
        ],
        "token_budget_min_reserve": float,  # min remaining budget fraction (0-1)
    }

Expected context keys used here:
    "active_session": bool               # she is actively in a CC/chat session
    "messages_sent_today": int           # integration-counted, from wake log
    "token_budget_remaining_fraction": float  # integration-computed from audit_log
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class GateResult:
    name: str
    allowed: bool
    reason: str


def gate_active_suspend(state, context: dict, config: dict, now: datetime) -> GateResult:
    if context.get("active_session"):
        return GateResult("active-suspend", False, "she is actively in a session")
    return GateResult("active-suspend", True, "no active session")


def gate_cooldown(state, context: dict, config: dict, now: datetime) -> GateResult:
    cooldown_min = config.get("gates", {}).get("cooldown_min", 0.0)
    last_wake_at = getattr(state, "last_wake_at", None)
    if last_wake_at is None or cooldown_min <= 0:
        return GateResult("cooldown", True, "no prior wake or no cooldown configured")

    elapsed_min = (now - last_wake_at).total_seconds() / 60.0
    if elapsed_min < cooldown_min:
        return GateResult(
            "cooldown", False, f"{elapsed_min:.1f}min < {cooldown_min:.1f}min cooldown"
        )
    return GateResult("cooldown", True, f"{elapsed_min:.1f}min since last wake")


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


def gate_fatigue_window(state, context: dict, config: dict, now: datetime) -> GateResult:
    windows = config.get("gates", {}).get("fatigue_windows", [])
    now_time = now.timetz().replace(tzinfo=None)

    for window in windows:
        start = _parse_hhmm(window["start"])
        end = _parse_hhmm(window["end"])
        if _in_window(now_time, start, end):
            return GateResult(
                "fatigue-window", False, f"within {window['start']}-{window['end']}"
            )
    return GateResult("fatigue-window", True, "outside fatigue windows")


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
    gate_active_suspend,
    gate_cooldown,
    gate_daily_cap,
    gate_fatigue_window,
    gate_token_budget,
)


def run_gates(state, context: dict, config: dict, now: datetime) -> list[GateResult]:
    """Run every gate (no short-circuit) so all results are available for logging."""
    return [gate(state, context, config, now) for gate in ALL_GATES]
