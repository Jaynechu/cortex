"""Wake-reason evaluation (Decided 07-02): event, affect flag, desire
threshold, self-scheduled, floor. Returns fired reasons carrying facts —
never pre-written motive lines (Design: reasoning happens in the cortex
session, not here).

Expected config shape (config["triggers"]):
    {
        "desire_thresholds": {"attachment": 0.8, "curiosity": 0.7, ...},
        "floor_interval_min": 60,
        "floor_jitter_min": 10,   # +/- jitter applied on reschedule
    }

Expected context keys used here:
    "events": list[dict]              # unprocessed events, each any shape
    "affect_flag": dict | None         # truthy = fired, passed through as facts
    "self_scheduled": list[dict]       # each has "due_at": datetime (tz-aware)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TriggerReason:
    kind: str
    detail: str
    facts: dict = field(default_factory=dict)


def _desire_triggers(desire_state, config: dict) -> list[TriggerReason]:
    thresholds = config.get("triggers", {}).get("desire_thresholds", {})
    reasons = []
    for name, threshold in thresholds.items():
        value = getattr(desire_state, name, None)
        if value is None:
            continue
        if value >= threshold:
            reasons.append(
                TriggerReason(
                    kind="desire",
                    detail=f"desire.{name} {value:.2f}>={threshold:.2f}",
                    facts={"dimension": name, "value": value, "threshold": threshold},
                )
            )
    return reasons


def _event_triggers(context: dict) -> list[TriggerReason]:
    events = context.get("events") or []
    return [
        TriggerReason(kind="event", detail=f"event: {event}", facts=dict(event))
        for event in events
    ]


def _affect_flag_trigger(context: dict) -> list[TriggerReason]:
    flag = context.get("affect_flag")
    if not flag:
        return []
    return [TriggerReason(kind="affect_flag", detail=f"affect flag: {flag}", facts=dict(flag))]


def _self_scheduled_triggers(context: dict, now: datetime) -> list[TriggerReason]:
    items = context.get("self_scheduled") or []
    reasons = []
    for item in items:
        due_at = item.get("due_at")
        if due_at is not None and due_at <= now:
            reasons.append(
                TriggerReason(kind="self_scheduled", detail=f"self-scheduled: {item}", facts=dict(item))
            )
    return reasons


def _floor_trigger(next_floor_due_at: datetime | None, now: datetime) -> list[TriggerReason]:
    if next_floor_due_at is None or now >= next_floor_due_at:
        return [
            TriggerReason(
                kind="floor",
                detail="floor check due",
                facts={"due_at": next_floor_due_at},
            )
        ]
    return []


def evaluate(
    desire_state,
    context: dict,
    config: dict,
    now: datetime,
    next_floor_due_at: datetime | None,
) -> list[TriggerReason]:
    """Evaluate all trigger kinds. Pure; consumes no rng (floor rescheduling
    is a separate step, see reschedule_floor())."""
    reasons: list[TriggerReason] = []
    reasons.extend(_event_triggers(context))
    reasons.extend(_affect_flag_trigger(context))
    reasons.extend(_desire_triggers(desire_state, config))
    reasons.extend(_self_scheduled_triggers(context, now))
    reasons.extend(_floor_trigger(next_floor_due_at, now))
    return reasons


def reschedule_floor(now: datetime, config: dict, rng: random.Random) -> datetime:
    """Compute the next floor due time with jitter. Call only when the floor
    trigger has fired, so jitter is drawn once per firing (deterministic
    given the same rng state)."""
    trig_config = config.get("triggers", {})
    interval_min = trig_config.get("floor_interval_min", 60)
    jitter_min = trig_config.get("floor_jitter_min", 0)

    jitter = rng.uniform(-jitter_min, jitter_min) if jitter_min else 0.0
    return now + timedelta(minutes=interval_min + jitter)
