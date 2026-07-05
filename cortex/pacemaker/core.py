"""Single entry point: tick(state, context, config, now, rng) -> decision.

Deterministic given inputs (same state/context/config/now/rng state ->
same decision). No I/O, no wall-clock reads; now and rng are always
injected by the caller (launchd loop / integration layer).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime

from cortex.pacemaker import gates
from cortex.pacemaker.desire import DesireState
from cortex.pacemaker.desire import tick as desire_tick
from cortex.pacemaker.expect_reply import ExpectReplyState
from cortex.pacemaker.expect_reply import evaluate as expect_reply_evaluate
from cortex.pacemaker.expect_reply import next_check_at as expect_reply_next_check
from cortex.pacemaker.triggers import evaluate as evaluate_triggers
from cortex.pacemaker.triggers import reschedule_floor


@dataclass(frozen=True)
class PacemakerState:
    desire: DesireState = DesireState()
    expect_reply: ExpectReplyState = ExpectReplyState()
    next_floor_due_at: datetime | None = None
    last_wake_at: datetime | None = None
    # C-wm timing: lie-down = wake finished; floor clock redraws from here.
    last_lie_down_at: datetime | None = None
    # Night mode: capped desire/floor wakes used in the night keyed here.
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
    # 1. desire accrual/decay
    new_desire = desire_tick(state.desire, context, config, now)

    # 2. expect-reply check (may add worry + its own trigger reason)
    new_expect_reply, worry_delta, expect_reply_reason = expect_reply_evaluate(
        state.expect_reply, context, config, now
    )
    if worry_delta:
        new_desire = new_desire.with_value("worry", new_desire.worry + worry_delta)

    # 3. trigger evaluation (pure, no rng)
    reasons = evaluate_triggers(new_desire, context, config, now, state.next_floor_due_at)
    if expect_reply_reason is not None:
        reasons.append(expect_reply_reason)

    floor_fired = any(r.kind == "floor" for r in reasons)
    new_next_floor_due_at = state.next_floor_due_at
    if floor_fired:
        new_next_floor_due_at = reschedule_floor(now, config, rng)

    # 4. gates (see the fired trigger kinds — night mode piercing keys on them)
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

    # 5. next_check: soonest of floor timer / expect-reply check
    candidates = [new_next_floor_due_at]
    er_next = expect_reply_next_check(new_expect_reply, config)
    if er_next is not None:
        candidates.append(er_next)
    candidates = [c for c in candidates if c is not None]
    next_check = min(candidates) if candidates else None

    new_last_wake_at = now if wake else state.last_wake_at

    new_state = PacemakerState(
        desire=new_desire,
        expect_reply=new_expect_reply,
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
        "next_check": next_check,
        "explanation": _render_explanation(now, reasons, gated_by),
    }

    return decision, new_state
