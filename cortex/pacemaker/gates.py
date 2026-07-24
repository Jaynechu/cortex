"""Gate chain: each gate is a pure function (state, context, config, now)
-> GateResult. A wake is allowed only if every gate allows. One gate: the
daily token budget; every other spend protection is the 150k per-wake fuse +
wakeup note battery gauge.

Expected config shape (config["gates"]):
    {
        "daily_budget": {"tokens": 1_000_000},
    }

Expected context keys used here:
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
    gate_daily_budget,
)


def run_gates(state, context: dict, config: dict, now: datetime) -> list[GateResult]:
    """Run every gate (no short-circuit) so all results are available for logging."""
    return [gate(state, context, config, now) for gate in ALL_GATES]
