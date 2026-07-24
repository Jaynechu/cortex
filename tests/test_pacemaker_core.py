import random
from datetime import datetime, timedelta, timezone

from cortex.pacemaker.core import PacemakerState, tick

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "triggers": {
            "floor_min_min": 10,
            "floor_max_min": 55,
        },
        "gates": {},
    }


def test_floor_trigger_wakes_on_first_tick_with_no_gates():
    state = PacemakerState()
    context = {"cal_busy": False, "at_home": False}
    decision, new_state = tick(state, context, base_config(), NOW, random.Random(1))
    assert decision["wake"] is True
    assert any(r.kind == "floor" for r in decision["reasons"])
    assert new_state.next_floor_due_at > NOW
    assert new_state.last_wake_at == NOW


def test_floor_wake_when_due():
    state = PacemakerState(next_floor_due_at=NOW - timedelta(seconds=1))
    decision, _ = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is True
    assert any(r.kind == "floor" for r in decision["reasons"])


def test_no_wake_when_floor_not_due():
    state = PacemakerState(next_floor_due_at=NOW + timedelta(hours=1))
    decision, _ = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is False
    assert decision["reasons"] == []
    assert decision["gated_by"] == []


def test_determinism_same_inputs_same_decision():
    state = PacemakerState(next_floor_due_at=None)
    context = {"events": [{"id": 1}]}
    config = base_config()

    decision1, state1 = tick(state, context, config, NOW, random.Random(99))
    decision2, state2 = tick(state, context, config, NOW, random.Random(99))

    assert decision1["wake"] == decision2["wake"]
    assert decision1["explanation"] == decision2["explanation"]
    assert [r.detail for r in decision1["reasons"]] == [r.detail for r in decision2["reasons"]]
    assert state1 == state2


def test_next_check_is_next_floor_due():
    state = PacemakerState(next_floor_due_at=NOW - timedelta(seconds=1))
    decision, new_state = tick(state, {}, base_config(), NOW, random.Random(1))
    # floor fired -> redrawn; next_check mirrors the new floor due time
    assert decision["next_check"] == new_state.next_floor_due_at
    assert decision["next_check"] > NOW


