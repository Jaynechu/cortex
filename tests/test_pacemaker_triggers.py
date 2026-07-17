import random
from datetime import datetime, timedelta, timezone

from cortex.pacemaker.triggers import (
    clamp_next_wake_minutes, clamp_window_minutes, evaluate, reschedule_floor)

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "triggers": {
            "floor_min_min": 10,
            "floor_max_min": 55,
        },
        "wake": {
            "wait_min": 1,
            "wait_max": 20,
            "next_wake_min": 21,
            "next_wake_max": 240,
        },
    }


def test_event_trigger_fires_per_event():
    context = {"events": [{"id": 1}, {"id": 2}]}
    reasons = evaluate(context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    event_reasons = [r for r in reasons if r.kind == "event"]
    assert len(event_reasons) == 2


def test_affect_flag_trigger_fires():
    context = {"affect_flag": {"word": "sad", "intensity": 4}}
    reasons = evaluate(context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert any(r.kind == "affect_flag" for r in reasons)


def test_affect_flag_absent_no_trigger():
    reasons = evaluate({}, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert not any(r.kind == "affect_flag" for r in reasons)


def test_floor_silent_when_pierce_source_fires():
    # Floor due AND an event fires -> single wake, floor stays silent.
    context = {"events": [{"id": 1}]}
    reasons = evaluate(context, base_config(), NOW, next_floor_due_at=NOW - timedelta(seconds=1))
    assert any(r.kind == "event" for r in reasons)
    assert not any(r.kind == "floor" for r in reasons)


def test_pierce_source_fires_even_when_floor_not_due():
    # event/affect_flag/self_scheduled fire regardless of the floor.
    context = {"events": [{"id": 1}]}
    reasons = evaluate(context, base_config(), NOW, next_floor_due_at=NOW + timedelta(minutes=30))
    assert any(r.kind == "event" for r in reasons)


def test_self_scheduled_trigger_fires_when_due():
    context = {"self_scheduled": [{"id": "x", "due_at": NOW - timedelta(minutes=1)}]}
    reasons = evaluate(context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert any(r.kind == "self_scheduled" for r in reasons)


def test_self_scheduled_no_trigger_when_not_yet_due():
    context = {"self_scheduled": [{"id": "x", "due_at": NOW + timedelta(minutes=1)}]}
    reasons = evaluate(context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert not any(r.kind == "self_scheduled" for r in reasons)


def test_floor_trigger_fires_when_none():
    reasons = evaluate({}, base_config(), NOW, next_floor_due_at=None)
    assert any(r.kind == "floor" for r in reasons)


def test_floor_trigger_fires_when_due():
    reasons = evaluate({}, base_config(), NOW, next_floor_due_at=NOW - timedelta(seconds=1))
    assert any(r.kind == "floor" for r in reasons)


def test_floor_trigger_silent_when_not_due():
    reasons = evaluate({}, base_config(), NOW, next_floor_due_at=NOW + timedelta(minutes=1))
    assert not any(r.kind == "floor" for r in reasons)


def test_reschedule_floor_within_uniform_bounds():
    rng = random.Random(42)
    config = base_config()
    next_due = reschedule_floor(NOW, config, rng)
    delta_min = (next_due - NOW).total_seconds() / 60.0
    assert 10.0 <= delta_min <= 55.0


def test_reschedule_floor_deterministic_with_seeded_rng():
    config = base_config()
    first = reschedule_floor(NOW, config, random.Random(7))
    second = reschedule_floor(NOW, config, random.Random(7))
    assert first == second


def test_reschedule_floor_defaults_when_config_missing():
    config = {"triggers": {}}
    rng = random.Random(1)
    next_due = reschedule_floor(NOW, config, rng)
    delta_min = (next_due - NOW).total_seconds() / 60.0
    assert 10.0 <= delta_min <= 55.0  # falls back to default 10/55 bounds


def test_reschedule_floor_fixed_bounds_when_equal():
    config = {"triggers": {"floor_min_min": 30, "floor_max_min": 30}}
    rng = random.Random(1)
    next_due = reschedule_floor(NOW, config, rng)
    assert next_due == NOW + timedelta(minutes=30)


def test_reschedule_floor_explicit_minutes_ignores_rng():
    # An explicit choice bypasses the dice; rng state is irrelevant.
    config = base_config()
    next_due = reschedule_floor(NOW, config, random.Random(1), minutes=20)
    assert next_due == NOW + timedelta(minutes=20)


def test_reschedule_floor_explicit_minutes_no_reclamp():
    # reschedule_floor no longer re-clamps: the caller (lie_down) already
    # clamped to [1, next_wake_max]. An explicit value is used verbatim.
    config = base_config()
    next_due = reschedule_floor(NOW, config, random.Random(1), minutes=999)
    assert next_due == NOW + timedelta(minutes=999)
    next_due = reschedule_floor(NOW, config, random.Random(1), minutes=1)
    assert next_due == NOW + timedelta(minutes=1)


def test_clamp_window_minutes_uses_wait_bounds():
    # wait(N) clamp now reads [wake.wait_min, wake.wait_max], decoupled from
    # the floor draw window.
    config = base_config()
    assert clamp_window_minutes(10, config) == 10
    assert clamp_window_minutes(0, config) == 1    # below wait_min -> 1
    assert clamp_window_minutes(90, config) == 20  # above wait_max -> 20


def test_clamp_next_wake_minutes_bounds():
    # Normal clamp = [next_wake_min (21), next_wake_max (240)].
    config = base_config()
    assert clamp_next_wake_minutes(120, config) == 120
    assert clamp_next_wake_minutes(10, config) == 21        # below floor -> 21
    assert clamp_next_wake_minutes(999, config) == 240      # above ceiling -> 240


def test_clamp_next_wake_minutes_rotate_no_longer_lowers_floor():
    # rotate=True no longer lowers the floor: the day floor (next_wake_min) is
    # already low enough. rotate uses the SAME [next_wake_min, next_wake_max].
    config = base_config()
    assert clamp_next_wake_minutes(30, config, rotate=True) == 30    # in-range kept
    assert clamp_next_wake_minutes(10, config, rotate=True) == 21    # below floor -> 21
    assert clamp_next_wake_minutes(999, config, rotate=True) == 240
