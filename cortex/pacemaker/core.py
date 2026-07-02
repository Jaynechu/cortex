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

    # 4. gates
    gate_results = gates.run_gates(state, context, config, now)
    gated_by = [g for g in gate_results if not g.allowed]

    wake = bool(reasons) and not gated_by

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
    )

    decision = {
        "wake": wake,
        "reasons": reasons,
        "gated_by": gated_by,
        "next_check": next_check,
        "explanation": _render_explanation(now, reasons, gated_by),
    }

    return decision, new_state
