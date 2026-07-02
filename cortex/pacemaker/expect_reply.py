"""Outbound-expecting-answer tracking (Decided 07-03): once cortex sends a
message that expects a reply, self-schedule a check (~30min config);
repeated silence raises worry and escalates tone level. Multi-message ok
(cache-cheap) — this module does not limit how many times start() is called.

Expected config shape (config["expect_reply"]):
    {
        "check_interval_min": 30,
        "worry_increment": 0.05,
        "tone_levels": ["neutral", "concerned", "worried", "anxious"],
    }

Expected context keys used here:
    "replied": bool   # she has replied since the pending message was sent
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from cortex.pacemaker.triggers import TriggerReason


@dataclass(frozen=True)
class ExpectReplyState:
    pending: bool = False
    sent_at: datetime | None = None
    last_check_at: datetime | None = None
    checks_done: int = 0
    tone_level: int = 0


def start(now: datetime) -> ExpectReplyState:
    """A message expecting a reply was just sent."""
    return ExpectReplyState(pending=True, sent_at=now, last_check_at=now, checks_done=0, tone_level=0)


def evaluate(
    state: ExpectReplyState, context: dict, config: dict, now: datetime
) -> tuple[ExpectReplyState, float, TriggerReason | None]:
    """Returns (new_state, worry_delta, trigger_reason_or_none)."""
    if not state.pending:
        return state, 0.0, None

    if context.get("replied"):
        return ExpectReplyState(), 0.0, None

    er_config = config.get("expect_reply", {})
    interval_min = er_config.get("check_interval_min", 30)

    if state.last_check_at is not None:
        due_at = state.last_check_at + timedelta(minutes=interval_min)
        if now < due_at:
            return state, 0.0, None

    tone_levels = er_config.get("tone_levels", ["neutral"])
    checks_done = state.checks_done + 1
    tone_level = min(state.tone_level + 1, len(tone_levels) - 1)
    worry_increment = er_config.get("worry_increment", 0.0)

    new_state = ExpectReplyState(
        pending=True,
        sent_at=state.sent_at,
        last_check_at=now,
        checks_done=checks_done,
        tone_level=tone_level,
    )
    tone_name = tone_levels[tone_level]
    reason = TriggerReason(
        kind="expect_reply",
        detail=f"no reply after {checks_done} check(s), tone={tone_name}",
        facts={"checks_done": checks_done, "tone_level": tone_level, "tone_name": tone_name},
    )
    return new_state, worry_increment, reason


def next_check_at(state: ExpectReplyState, config: dict) -> datetime | None:
    """When the next expect-reply check is due, or None if not pending."""
    if not state.pending or state.last_check_at is None:
        return None
    interval_min = config.get("expect_reply", {}).get("check_interval_min", 30)
    return state.last_check_at + timedelta(minutes=interval_min)
