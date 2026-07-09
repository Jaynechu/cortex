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
        "gates": {
            "night": {"start": "00:00", "end": "06:00", "cap": 1},
        },
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


def test_explanation_string_contains_gate_names_when_blocked():
    # Night cap reached -> floor wake is gated, gate name appears in explanation.
    state = PacemakerState(
        next_floor_due_at=NIGHT_NOW - timedelta(seconds=1),
        night_cap_key="2026-07-04",
        night_wake_count=1,
    )
    decision, _ = tick(state, {}, base_config(), NIGHT_NOW, random.Random(1))
    assert decision["wake"] is False
    assert "gated:" in decision["explanation"]
    assert "night-mode" in decision["explanation"]


# --- night wake counter -------------------------------------------------------

NIGHT_NOW = datetime(2026, 7, 4, 2, 0, tzinfo=TZ)  # inside default 00:00-06:00 window


def test_tick_increments_night_counter_on_capped_kind_wake():
    state = PacemakerState(
        next_floor_due_at=NIGHT_NOW - timedelta(seconds=1),
        night_cap_key="2026-07-04",
        night_wake_count=0,
    )
    decision, new_state = tick(state, {}, base_config(), NIGHT_NOW, random.Random(1))
    assert decision["wake"] is True  # count 0 < cap 1, still allowed this once
    assert any(r.kind == "floor" for r in decision["reasons"])
    assert new_state.night_cap_key == "2026-07-04"
    assert new_state.night_wake_count == 1


def test_tick_resets_night_counter_on_new_night():
    state = PacemakerState(
        next_floor_due_at=NIGHT_NOW - timedelta(seconds=1),
        night_cap_key="2026-07-03",  # stale, belongs to a previous night
        night_wake_count=5,
    )
    decision, new_state = tick(state, {}, base_config(), NIGHT_NOW, random.Random(1))
    assert new_state.night_cap_key == "2026-07-04"
    assert new_state.night_wake_count == 1  # reset to 0, then incremented once


def test_tick_pierce_wake_does_not_increment_night_counter():
    state = PacemakerState(
        night_cap_key="2026-07-04",
        night_wake_count=0,
        next_floor_due_at=NIGHT_NOW + timedelta(hours=1),
    )
    context = {"schedule": [{"name": "duty"}]}
    decision, new_state = tick(state, context, base_config(), NIGHT_NOW, random.Random(1))
    assert decision["wake"] is True
    assert any(r.kind == "schedule" for r in decision["reasons"])
    assert new_state.night_wake_count == 0  # schedule pierces, doesn't consume cap


def test_tick_night_counter_untouched_outside_window():
    state = PacemakerState(
        next_floor_due_at=NOW - timedelta(seconds=1),
        night_cap_key=None,
        night_wake_count=0,
    )
    decision, new_state = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is True
    assert new_state.night_cap_key is None
    assert new_state.night_wake_count == 0
