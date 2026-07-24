"""Single entry point: tick(state, context, config, now, rng) -> decision.

Deterministic given inputs (same state/context/config/now/rng state ->
same decision). No I/O, no wall-clock reads; now and rng are always
injected by the caller (launchd loop / integration layer).
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from cortex.pacemaker import gates
from cortex.pacemaker.triggers import evaluate as evaluate_triggers
from cortex.pacemaker.triggers import reschedule_floor


@dataclass(frozen=True)
class PacemakerState:
    next_floor_due_at: datetime | None = None
    last_wake_at: datetime | None = None
    # C-wm timing: lie-down = wake finished; floor clock redraws from here.
    last_lie_down_at: datetime | None = None
    # Cortex session resume (C3). Opaque to tick() — only the wake caller
    # (cortex.wake) reads/writes this.
    cortex_session_id: str | None = None


def _render_explanation(now: datetime, reasons: list, gated: list) -> str:
    stamp = now.strftime("%H:%M")
    if reasons:
        reason_text = "; ".join(r.detail for r in reasons)
    else:
        reason_text = "no reasons fired"
    line = f"{stamp} wake: {reason_text}" if reasons else f"{stamp} no wake: {reason_text}"
    if gated:
        line += "; gated: " + ", ".join(g.name for g in gated)
    return line


def tick(
    state: PacemakerState,
    context: dict,
    config: dict,
    now: datetime,
    rng: random.Random,
) -> tuple[dict, PacemakerState]:
    # 1. trigger evaluation (pure, no rng)
    reasons = evaluate_triggers(context, config, now, state.next_floor_due_at)

    floor_fired = any(r.kind == "floor" for r in reasons)
    new_next_floor_due_at = state.next_floor_due_at
    if floor_fired:
        new_next_floor_due_at = reschedule_floor(now, config, rng)

    # 2. gates
    gate_results = gates.run_gates(state, context, config, now)
    gated_by = [g for g in gate_results if not g.allowed]

    wake = bool(reasons) and not gated_by

    new_last_wake_at = now if wake else state.last_wake_at

    new_state = PacemakerState(
        next_floor_due_at=new_next_floor_due_at,
        last_wake_at=new_last_wake_at,
        last_lie_down_at=state.last_lie_down_at,
        cortex_session_id=state.cortex_session_id,
    )

    decision = {
        "wake": wake,
        "reasons": reasons,
        "gated_by": gated_by,
        "next_check": new_next_floor_due_at,
        "explanation": _render_explanation(now, reasons, gated_by),
    }

    return decision, new_state
