import random
from datetime import datetime, timedelta, timezone

from cortex.pacemaker.desire import DesireState
from cortex.pacemaker.triggers import evaluate, reschedule_floor

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "triggers": {
            "desire_thresholds": {"attachment": 0.8, "curiosity": 0.7},
            "floor_interval_min": 60,
            "floor_jitter_min": 10,
        }
    }


def test_event_trigger_fires_per_event():
    context = {"events": [{"id": 1}, {"id": 2}]}
    reasons = evaluate(DesireState(), context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    event_reasons = [r for r in reasons if r.kind == "event"]
    assert len(event_reasons) == 2


def test_affect_flag_trigger_fires():
    context = {"affect_flag": {"word": "sad", "intensity": 4}}
    reasons = evaluate(DesireState(), context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert any(r.kind == "affect_flag" for r in reasons)


def test_affect_flag_absent_no_trigger():
    reasons = evaluate(DesireState(), {}, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert not any(r.kind == "affect_flag" for r in reasons)


def test_desire_threshold_trigger_fires_above():
    state = DesireState(attachment=0.85)
    reasons = evaluate(state, {}, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    desire_reasons = [r for r in reasons if r.kind == "desire"]
    assert len(desire_reasons) == 1
    assert desire_reasons[0].facts["dimension"] == "attachment"


def test_desire_threshold_no_trigger_below():
    state = DesireState(attachment=0.5)
    reasons = evaluate(state, {}, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert not any(r.kind == "desire" for r in reasons)


def test_self_scheduled_trigger_fires_when_due():
    context = {"self_scheduled": [{"id": "x", "due_at": NOW - timedelta(minutes=1)}]}
    reasons = evaluate(DesireState(), context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert any(r.kind == "self_scheduled" for r in reasons)


def test_self_scheduled_no_trigger_when_not_yet_due():
    context = {"self_scheduled": [{"id": "x", "due_at": NOW + timedelta(minutes=1)}]}
    reasons = evaluate(DesireState(), context, base_config(), NOW, next_floor_due_at=NOW + timedelta(hours=1))
    assert not any(r.kind == "self_scheduled" for r in reasons)


def test_floor_trigger_fires_when_none():
    reasons = evaluate(DesireState(), {}, base_config(), NOW, next_floor_due_at=None)
    assert any(r.kind == "floor" for r in reasons)


def test_floor_trigger_fires_when_due():
    reasons = evaluate(DesireState(), {}, base_config(), NOW, next_floor_due_at=NOW - timedelta(seconds=1))
    assert any(r.kind == "floor" for r in reasons)


def test_floor_trigger_silent_when_not_due():
    reasons = evaluate(DesireState(), {}, base_config(), NOW, next_floor_due_at=NOW + timedelta(minutes=1))
    assert not any(r.kind == "floor" for r in reasons)


def test_reschedule_floor_within_jitter_bounds():
    rng = random.Random(42)
    config = base_config()
    next_due = reschedule_floor(NOW, config, rng)
    delta_min = (next_due - NOW).total_seconds() / 60.0
    assert 50.0 <= delta_min <= 70.0  # 60 +/- 10


def test_reschedule_floor_deterministic_with_seeded_rng():
    config = base_config()
    first = reschedule_floor(NOW, config, random.Random(7))
    second = reschedule_floor(NOW, config, random.Random(7))
    assert first == second


def test_reschedule_floor_no_jitter_config():
    config = {"triggers": {"floor_interval_min": 60, "floor_jitter_min": 0}}
    rng = random.Random(1)
    next_due = reschedule_floor(NOW, config, rng)
    assert next_due == NOW + timedelta(minutes=60)
