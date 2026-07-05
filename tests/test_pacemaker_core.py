import random
from datetime import datetime, timedelta, timezone

from cortex.pacemaker.core import PacemakerState, tick
from cortex.pacemaker.desire import DesireState
from cortex.pacemaker.expect_reply import start as expect_reply_start

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "desire": {
            "attachment": {
                "base_rate_per_min": 0.01,
                "decay_rate_per_min": 0.001,
                "busy_multiplier": 0.0,
                "home_free_multiplier": 3.0,
                "gap_threshold_min": 60,
            },
            "curiosity": {"base_rate_per_min": 0.005, "decay_rate_per_min": 0.001},
            "worry": {"base_rate_per_min": 0.0, "decay_rate_per_min": 0.0},
            "duty": {"base_rate_per_min": 0.003, "decay_rate_per_min": 0.0005},
        },
        "triggers": {
            "desire_thresholds": {"attachment": 0.8, "curiosity": 0.7},
            "floor_min_min": 10,
            "floor_max_min": 55,
        },
        "gates": {
            "night": {"start": "00:00", "end": "06:00", "cap": 1},
        },
        "expect_reply": {
            "check_interval_min": 30,
            "worry_increment": 0.05,
            "tone_levels": ["neutral", "concerned", "worried", "anxious"],
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


def test_desire_threshold_wake_via_tick():
    # Desire motivates a wake only once the floor is due (floor governs desire).
    state = PacemakerState(
        desire=DesireState(attachment=0.85, last_tick_at=NOW),
        next_floor_due_at=NOW - timedelta(seconds=1),
    )
    decision, _ = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is True
    assert any(r.kind == "desire" for r in decision["reasons"])


def test_desire_held_behind_floor_no_wake():
    # Over threshold but floor not due -> desire is held, no wake.
    state = PacemakerState(
        desire=DesireState(attachment=0.85, last_tick_at=NOW),
        next_floor_due_at=NOW + timedelta(hours=1),
    )
    decision, _ = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is False
    assert decision["reasons"] == []


def test_no_reasons_no_wake():
    state = PacemakerState(
        desire=DesireState(attachment=0.1, last_tick_at=NOW),
        next_floor_due_at=NOW + timedelta(hours=1),
    )
    decision, _ = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is False
    assert decision["reasons"] == []
    assert decision["gated_by"] == []


def test_expect_reply_escalation_feeds_worry_and_reasons():
    state = PacemakerState(
        expect_reply=expect_reply_start(NOW),
        next_floor_due_at=NOW + timedelta(hours=1),
    )
    later = NOW + timedelta(minutes=30)
    decision, new_state = tick(state, {}, base_config(), later, random.Random(1))
    assert any(r.kind == "expect_reply" for r in decision["reasons"])
    assert new_state.desire.worry == 0.05
    assert new_state.expect_reply.tone_level == 1


def test_determinism_same_inputs_same_decision():
    state = PacemakerState(
        desire=DesireState(attachment=0.5, last_tick_at=NOW),
        next_floor_due_at=None,
    )
    context = {"events": [{"id": 1}]}
    config = base_config()

    decision1, state1 = tick(state, context, config, NOW, random.Random(99))
    decision2, state2 = tick(state, context, config, NOW, random.Random(99))

    assert decision1["wake"] == decision2["wake"]
    assert decision1["explanation"] == decision2["explanation"]
    assert [r.detail for r in decision1["reasons"]] == [r.detail for r in decision2["reasons"]]
    assert state1 == state2


def test_next_check_uses_soonest_of_floor_and_expect_reply():
    state = PacemakerState(
        expect_reply=expect_reply_start(NOW),
        next_floor_due_at=NOW + timedelta(hours=2),
    )
    decision, new_state = tick(state, {}, base_config(), NOW, random.Random(1))
    # expect-reply next check (30min) is sooner than floor (2h)
    assert decision["next_check"] == NOW + timedelta(minutes=30)


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
        desire=DesireState(attachment=0.85, last_tick_at=NIGHT_NOW),
        night_cap_key="2026-07-04",
        night_wake_count=0,
    )
    decision, new_state = tick(state, {}, base_config(), NIGHT_NOW, random.Random(1))
    assert decision["wake"] is True  # count 0 < cap 1, still allowed this once
    assert any(r.kind == "desire" for r in decision["reasons"])
    assert new_state.night_cap_key == "2026-07-04"
    assert new_state.night_wake_count == 1


def test_tick_resets_night_counter_on_new_night():
    state = PacemakerState(
        desire=DesireState(attachment=0.85, last_tick_at=NIGHT_NOW),
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
    context = {"events": [{"id": 1}]}
    decision, new_state = tick(state, context, base_config(), NIGHT_NOW, random.Random(1))
    assert decision["wake"] is True
    assert any(r.kind == "event" for r in decision["reasons"])
    assert new_state.night_wake_count == 0  # event pierces, doesn't consume cap


def test_tick_night_counter_untouched_outside_window():
    state = PacemakerState(
        desire=DesireState(attachment=0.85, last_tick_at=NOW),
        night_cap_key=None,
        night_wake_count=0,
    )
    decision, new_state = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is True
    assert new_state.night_cap_key is None
    assert new_state.night_wake_count == 0
