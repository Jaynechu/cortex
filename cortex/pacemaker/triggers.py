"""Wake-reason evaluation (Decided 07-02): event, affect flag, self-scheduled,
schedule, floor. Returns fired reasons carrying facts — never pre-written motive
lines (Design: reasoning happens in the cortex session, not here).

Expected config shape (config["triggers"]):
    {
        "floor_min_min": 10,   # wake-window draw lower bound (minutes)
        "floor_max_min": 55,   # wake-window draw upper bound (minutes)
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


def _schedule_triggers(context: dict) -> list[TriggerReason]:
    """Fixed recurring duties already resolved to due-and-unfired by the
    integration layer (see pacemaker.schedule.due_duties)."""
    items = context.get("schedule") or []
    return [
        TriggerReason(kind="schedule", detail=f"schedule: {item.get('name')}", facts=dict(item))
        for item in items
        if isinstance(item, dict)
    ]


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
    context: dict,
    config: dict,
    now: datetime,
    next_floor_due_at: datetime | None,
) -> list[TriggerReason]:
    """Evaluate all trigger kinds. Pure; consumes no rng (floor rescheduling
    is a separate step, see reschedule_floor()).

    Collision model (C-wm): the floor timer governs the plain heartbeat ONLY.
    event/affect_flag (trigger) and self_scheduled (schedule) pierce anytime
    and are never held back — ordered trigger > schedule. Coincident firings
    collapse to one wake; the plain floor heartbeat stays silent whenever any
    other source already fired this tick.
    """
    pierce: list[TriggerReason] = []
    pierce.extend(_event_triggers(context))
    pierce.extend(_affect_flag_trigger(context))
    pierce.extend(_self_scheduled_triggers(context, now))
    pierce.extend(_schedule_triggers(context))

    floor_due = next_floor_due_at is None or now >= next_floor_due_at
    if not floor_due:
        return pierce  # floor not due yet

    if pierce:
        return pierce  # something real fired -> floor silent
    return _floor_trigger(next_floor_due_at, now)


def clamp_window_minutes(minutes: float, config: dict) -> float:
    """Clamp an explicit wake choice to [floor_min_min, floor_max_min] — the
    min guards against thrash, the max protects the cache TTL."""
    trig_config = config.get("triggers", {})
    lo = trig_config.get("floor_min_min", 10)
    hi = trig_config.get("floor_max_min", 55)
    return max(lo, min(hi, minutes))


def reschedule_floor(now: datetime, config: dict, rng: random.Random,
                     minutes: float | None = None) -> datetime:
    """Draw the next wake due time from `now`. `minutes` = an explicit choice
    (clamped to [floor_min_min, floor_max_min]); None = a uniform "dice" draw
    within that window. Callers pass lie-down time as `now` on the wake path
    (C-wm: the clock runs from lie-down, not wake); gated firings redraw from
    tick time so a blocked floor doesn't re-fire every tick."""
    trig_config = config.get("triggers", {})
    lo = trig_config.get("floor_min_min", 10)
    hi = trig_config.get("floor_max_min", 55)
    draw = rng.uniform(lo, hi) if minutes is None else clamp_window_minutes(minutes, config)
    return now + timedelta(minutes=draw)
