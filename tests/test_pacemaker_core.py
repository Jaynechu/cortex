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
            "floor_interval_min": 60,
            "floor_jitter_min": 10,
        },
        "gates": {
            "cooldown_min": 20,
            "daily_message_cap": 5,
            "fatigue_windows": [{"start": "23:30", "end": "07:00"}],
            "token_budget_min_reserve": 0.1,
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


def test_active_session_gates_block_wake_despite_reasons():
    state = PacemakerState()
    context = {"active_session": True}
    decision, new_state = tick(state, context, base_config(), NOW, random.Random(1))
    assert decision["wake"] is False
    assert decision["reasons"]  # floor still fired
    assert any(g.name == "active-suspend" for g in decision["gated_by"])
    assert new_state.last_wake_at is None


def test_desire_threshold_wake_via_tick():
    state = PacemakerState(
        desire=DesireState(attachment=0.85, last_tick_at=NOW),
        next_floor_due_at=NOW + timedelta(hours=1),
    )
    decision, _ = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is True
    assert any(r.kind == "desire" for r in decision["reasons"])


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
    state = PacemakerState(desire=DesireState(attachment=0.85, last_tick_at=NOW))
    decision, _ = tick(state, {"active_session": True}, base_config(), NOW, random.Random(1))
    assert "gated:" in decision["explanation"]
    assert "active-suspend" in decision["explanation"]


def test_cooldown_gate_blocks_wake_shortly_after_previous_wake():
    state = PacemakerState(
        desire=DesireState(attachment=0.85, last_tick_at=NOW),
        last_wake_at=NOW - timedelta(minutes=5),
        next_floor_due_at=NOW + timedelta(hours=1),
    )
    decision, _ = tick(state, {}, base_config(), NOW, random.Random(1))
    assert decision["wake"] is False
    assert any(g.name == "cooldown" for g in decision["gated_by"])
