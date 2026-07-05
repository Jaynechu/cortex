from datetime import datetime, timedelta, timezone

from cortex.pacemaker import gates
from cortex.pacemaker.core import PacemakerState

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "gates": {
            "cooldown_min_min": 15,
            "cooldown_max_min": 20,
            "wake_stale_min": 30,
            "daily_message_cap": 5,
            "night": {"start": "00:00", "end": "06:00", "cap": 1},
            "token_budget_min_reserve": 0.1,
        }
    }


# --- cooldown ---------------------------------------------------------------

def test_cooldown_blocks_while_wake_in_progress():
    state = PacemakerState(last_wake_at=NOW - timedelta(minutes=5), last_lie_down_at=None)
    result = gates.gate_cooldown(state, {}, base_config(), NOW)
    assert result.allowed is False


def test_cooldown_wake_in_progress_stale_escape_falls_through():
    # last_wake_at older than wake_stale_min, never lain down -> presumed crashed
    state = PacemakerState(last_wake_at=NOW - timedelta(minutes=45), last_lie_down_at=None)
    result = gates.gate_cooldown(state, {}, base_config(), NOW)
    assert result.allowed is True


def test_cooldown_wake_in_progress_when_lie_down_predates_wake():
    # lain down from a previous wake, but a newer wake hasn't lain down yet
    state = PacemakerState(
        last_wake_at=NOW - timedelta(minutes=2),
        last_lie_down_at=NOW - timedelta(hours=1),
    )
    result = gates.gate_cooldown(state, {}, base_config(), NOW)
    assert result.allowed is False


def test_cooldown_blocks_when_cooldown_until_in_future():
    state = PacemakerState(
        last_wake_at=NOW - timedelta(minutes=40),
        last_lie_down_at=NOW - timedelta(minutes=35),
        cooldown_until=NOW + timedelta(minutes=10),
    )
    result = gates.gate_cooldown(state, {}, base_config(), NOW)
    assert result.allowed is False


def test_cooldown_allows_when_cooldown_until_in_past():
    state = PacemakerState(
        last_wake_at=NOW - timedelta(minutes=40),
        last_lie_down_at=NOW - timedelta(minutes=35),
        cooldown_until=NOW - timedelta(minutes=1),
    )
    result = gates.gate_cooldown(state, {}, base_config(), NOW)
    assert result.allowed is True


def test_cooldown_allows_when_cooldown_until_none_and_no_wake_in_progress():
    result = gates.gate_cooldown(PacemakerState(), {}, base_config(), NOW)
    assert result.allowed is True


# --- daily cap ---------------------------------------------------------------

def test_daily_cap_blocks_at_limit():
    result = gates.gate_daily_cap(PacemakerState(), {"messages_sent_today": 5}, base_config(), NOW)
    assert result.allowed is False


def test_daily_cap_allows_below_limit():
    result = gates.gate_daily_cap(PacemakerState(), {"messages_sent_today": 2}, base_config(), NOW)
    assert result.allowed is True


# --- night mode ---------------------------------------------------------------

NIGHT_NOW = datetime(2026, 7, 4, 2, 0, tzinfo=TZ)  # inside 00:00-06:00


def test_night_mode_allows_outside_window():
    result = gates.gate_night_mode(PacemakerState(), {"trigger_kinds": ["floor"]}, base_config(), NOW)
    assert result.allowed is True


def test_night_mode_allows_pierce_kind_at_cap():
    state = PacemakerState(night_cap_key="2026-07-04", night_wake_count=1)
    result = gates.gate_night_mode(
        state, {"trigger_kinds": ["event"]}, base_config(), NIGHT_NOW
    )
    assert result.allowed is True


def test_night_mode_blocks_capped_kind_at_cap():
    state = PacemakerState(night_cap_key="2026-07-04", night_wake_count=1)
    result = gates.gate_night_mode(
        state, {"trigger_kinds": ["floor"]}, base_config(), NIGHT_NOW
    )
    assert result.allowed is False


def test_night_mode_stale_cap_key_treated_as_fresh_night():
    # night_cap_key belongs to a previous night -> counter resets to 0
    state = PacemakerState(night_cap_key="2026-07-03", night_wake_count=5)
    result = gates.gate_night_mode(
        state, {"trigger_kinds": ["floor"]}, base_config(), NIGHT_NOW
    )
    assert result.allowed is True


def test_night_mode_blocks_desire_floor_expect_reply_once_capped():
    state = PacemakerState(night_cap_key="2026-07-04", night_wake_count=1)
    for kind in ("desire", "floor", "expect_reply"):
        result = gates.gate_night_mode(state, {"trigger_kinds": [kind]}, base_config(), NIGHT_NOW)
        assert result.allowed is False, kind


def test_night_mode_allows_below_cap():
    state = PacemakerState(night_cap_key="2026-07-04", night_wake_count=0)
    result = gates.gate_night_mode(state, {"trigger_kinds": ["floor"]}, base_config(), NIGHT_NOW)
    assert result.allowed is True


def test_night_key_none_outside_window():
    assert gates.night_key(base_config(), NOW) is None


def test_night_key_present_inside_window():
    assert gates.night_key(base_config(), NIGHT_NOW) == "2026-07-04"


# --- token budget ---------------------------------------------------------------

def test_token_budget_blocks_below_reserve():
    result = gates.gate_token_budget(
        PacemakerState(), {"token_budget_remaining_fraction": 0.05}, base_config(), NOW
    )
    assert result.allowed is False


def test_token_budget_allows_above_reserve():
    result = gates.gate_token_budget(
        PacemakerState(), {"token_budget_remaining_fraction": 0.5}, base_config(), NOW
    )
    assert result.allowed is True


# --- run_gates ---------------------------------------------------------------

def test_run_gates_returns_all_results_no_short_circuit():
    context = {"messages_sent_today": 5, "trigger_kinds": ["floor"]}
    results = gates.run_gates(PacemakerState(), context, base_config(), NOW)
    assert len(results) == 4
    names = {r.name for r in results if not r.allowed}
    assert "daily-cap" in names
