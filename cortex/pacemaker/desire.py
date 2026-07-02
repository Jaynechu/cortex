"""Desire state: 4 floats that accrue/decay per tick, clamped to [0, 1].

Dimensions (config-labelled, named here for clarity):
- attachment: pull to reach out to her. Context-modulated (see tick()).
- curiosity: pull to explore/learn something and tell her.
- worry: concern building from silence / unresolved threads.
- duty: pull from scheduled/owed items (reminders, promises, follow-ups).

Expected config shape (config["desire"][<name>]):
    {
        "base_rate_per_min": float,   # accrual per minute at baseline
        "decay_rate_per_min": float,  # decay per minute (always applied)
        # attachment only:
        "busy_multiplier": float,        # applied when context["cal_busy"]
        "home_free_multiplier": float,   # applied when home+free+long gap
        "gap_threshold_min": float,      # min gap since last real chat
    }

Expected context keys used here:
    "cal_busy": bool
    "at_home": bool
    "last_real_chat_at": datetime | None (tz-aware)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

DIMENSIONS = ("attachment", "curiosity", "worry", "duty")


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class DesireState:
    attachment: float = 0.0
    curiosity: float = 0.0
    worry: float = 0.0
    duty: float = 0.0
    last_tick_at: datetime | None = None

    def get(self, name: str) -> float:
        return getattr(self, name)

    def with_value(self, name: str, value: float) -> "DesireState":
        return replace(self, **{name: clamp(value)})


def _attachment_multiplier(context: dict, dim_config: dict, now: datetime) -> float:
    """Context modulation for attachment accrual only (Decided 07-03)."""
    if context.get("cal_busy"):
        return dim_config.get("busy_multiplier", 0.0)

    last_chat = context.get("last_real_chat_at")
    gap_min = float("inf")
    if last_chat is not None:
        gap_min = (now - last_chat).total_seconds() / 60.0

    gap_threshold = dim_config.get("gap_threshold_min", float("inf"))
    if context.get("at_home") and gap_min >= gap_threshold:
        return dim_config.get("home_free_multiplier", 1.0)

    return 1.0


def tick(state: DesireState, context: dict, config: dict, now: datetime) -> DesireState:
    """Apply one tick of decay+accrual to all four dimensions. Pure."""
    desire_config = config.get("desire", {})

    if state.last_tick_at is None:
        dt_minutes = 0.0
    else:
        dt_minutes = max(0.0, (now - state.last_tick_at).total_seconds() / 60.0)

    values = {}
    for dim in DIMENSIONS:
        dim_config = desire_config.get(dim, {})
        base_rate = dim_config.get("base_rate_per_min", 0.0)
        decay_rate = dim_config.get("decay_rate_per_min", 0.0)

        multiplier = 1.0
        if dim == "attachment":
            multiplier = _attachment_multiplier(context, dim_config, now)

        delta = (base_rate * multiplier - decay_rate) * dt_minutes
        values[dim] = clamp(state.get(dim) + delta)

    return DesireState(last_tick_at=now, **values)
