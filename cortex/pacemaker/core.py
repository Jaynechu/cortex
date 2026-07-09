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
    # Night mode: capped floor wakes used in the night keyed here.
    night_cap_key: str | None = None
    night_wake_count: int = 0
    # Cortex session resume (C3, Decided daily rebirth). Opaque to tick() —
    # only the wake caller (cortex.wake) reads/writes these.
    cortex_session_id: str | None = None
    cortex_session_date: str | None = None


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

    # 2. gates (see the fired trigger kinds — night mode piercing keys on them)
    gates_context = dict(context)
    gates_context["trigger_kinds"] = [r.kind for r in reasons]
    gate_results = gates.run_gates(state, gates_context, config, now)
    gated_by = [g for g in gate_results if not g.allowed]

    wake = bool(reasons) and not gated_by

    # Night cap accounting: a wake with no piercing trigger consumes the cap.
    new_night_cap_key = state.night_cap_key
    new_night_wake_count = state.night_wake_count
    current_night = gates.night_key(config, now)
    if current_night is not None and current_night != state.night_cap_key:
        new_night_cap_key, new_night_wake_count = current_night, 0
    if wake and current_night is not None:
        if not ({r.kind for r in reasons} & gates.PIERCE_KINDS):
            new_night_wake_count += 1

    new_last_wake_at = now if wake else state.last_wake_at

    new_state = PacemakerState(
        next_floor_due_at=new_next_floor_due_at,
        last_wake_at=new_last_wake_at,
        last_lie_down_at=state.last_lie_down_at,
        night_cap_key=new_night_cap_key,
        night_wake_count=new_night_wake_count,
        cortex_session_id=state.cortex_session_id,
        cortex_session_date=state.cortex_session_date,
    )

    decision = {
        "wake": wake,
        "reasons": reasons,
        "gated_by": gated_by,
        "next_check": new_next_floor_due_at,
        "explanation": _render_explanation(now, reasons, gated_by),
    }

    return decision, new_state
