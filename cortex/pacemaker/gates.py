"""Gate chain: each gate is a pure function (state, context, config, now)
-> GateResult. A wake is allowed only if every gate allows. Two gates: the
night window (23-06 zero self-wakes) and the daily token budget; every other
spend protection is the 150k per-wake fuse + wakeup note battery gauge.

Expected config shape (config["gates"]):
    {
        "night": {"start": "23:00", "end": "06:00", "cap": 0},
        "daily_budget": {"tokens": 1_000_000},
    }

Expected context keys used here:
    "today_tokens": int                  # Cortex Today: today's finished-window final occupancies + live window (integration)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta


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
    """Night window (23-06, cap 0 by default): every self-wake stays silent."""
    key = night_key(config, now)
    if key is None:
        return GateResult("night-mode", True, "outside night window")

    cap = _night_cfg(config).get("cap", 0)
    count = getattr(state, "night_wake_count", 0)
    if getattr(state, "night_cap_key", None) != key:
        count = 0  # counter belongs to a previous night
    if count >= cap:
        return GateResult("night-mode", False, f"night cap reached ({count}/{cap})")
    return GateResult("night-mode", True, f"night wake {count}/{cap} used")


def gate_daily_budget(state, context: dict, config: dict, now: datetime) -> GateResult:
    """Daily token budget: once today's wake-token spend (SUM ct_wake_log.tokens,
    supplied as context["today_tokens"]) reaches the cap, all self-wakes fall
    silent. Resets at local midnight (SUM is per-day)."""
    cap = int(config.get("gates", {}).get("daily_budget", {}).get("tokens", 1_000_000))
    if cap <= 0:
        return GateResult("daily_budget", True, "budget disabled")
    spent = int(context.get("today_tokens", 0) or 0)
    if spent >= cap:
        return GateResult("daily_budget", False, f"daily budget spent ({spent}/{cap})")
    return GateResult("daily_budget", True, f"budget {spent}/{cap} used")


ALL_GATES = (
    gate_night_mode,
    gate_daily_budget,
)


def run_gates(state, context: dict, config: dict, now: datetime) -> list[GateResult]:
    """Run every gate (no short-circuit) so all results are available for logging."""
    return [gate(state, context, config, now) for gate in ALL_GATES]
