"""Gate chain: each gate is a pure function (state, context, config, now)
-> GateResult. A wake is allowed only if every gate allows. Two gates: the
night cap (flag-based roaming ceiling) and the daily token budget; every other
spend protection is the 150k per-wake fuse + wakeup note battery gauge.

Expected config shape (config["gates"]):
    {
        "daily_budget": {"tokens": 1_000_000},
    }

Expected context keys used here:
    "mode": str | None                   # "night" = the flag is set (low-freq roaming)
    "today_tokens": int                  # Cortex Today: today's finished-window final occupancies + live window (integration)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class GateResult:
    name: str
    allowed: bool
    reason: str


def gate_night_cap(state, context: dict, config: dict, now: datetime) -> GateResult:
    """Night-flag cap: while the night flag is set, self-wakes are allowed but
    bounded by [night].cap per flag-set->clear night (a safety ceiling, not zero —
    roaming needs headroom). Day (no flag) -> always allowed. The per-night
    counter (night_cap_key / night_wake_count) is keyed on the flag lifecycle by
    core.tick, not on a clock window."""
    if context.get("mode") != "night":
        return GateResult("night-cap", True, "day (no night flag)")
    cap = int(config.get("night", {}).get("cap", 6))
    count = getattr(state, "night_wake_count", 0)
    if count >= cap:
        return GateResult("night-cap", False, f"night cap reached ({count}/{cap})")
    return GateResult("night-cap", True, f"night wake {count}/{cap} used")


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
    gate_night_cap,
    gate_daily_budget,
)


def run_gates(state, context: dict, config: dict, now: datetime) -> list[GateResult]:
    """Run every gate (no short-circuit) so all results are available for logging."""
    return [gate(state, context, config, now) for gate in ALL_GATES]
