from datetime import datetime, timedelta, timezone

from cortex.pacemaker import gates
from cortex.pacemaker.core import PacemakerState

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "gates": {
            "cooldown_min": 20,
            "daily_message_cap": 5,
            "fatigue_windows": [{"start": "23:30", "end": "07:00"}],
            "token_budget_min_reserve": 0.1,
        }
    }


def test_active_suspend_blocks():
    result = gates.gate_active_suspend(PacemakerState(), {"active_session": True}, base_config(), NOW)
    assert result.allowed is False


def test_active_suspend_allows_when_idle():
    result = gates.gate_active_suspend(PacemakerState(), {"active_session": False}, base_config(), NOW)
    assert result.allowed is True


def test_cooldown_blocks_recent_wake():
    state = PacemakerState(last_wake_at=NOW - timedelta(minutes=5))
    result = gates.gate_cooldown(state, {}, base_config(), NOW)
    assert result.allowed is False


def test_cooldown_allows_after_interval():
    state = PacemakerState(last_wake_at=NOW - timedelta(minutes=25))
    result = gates.gate_cooldown(state, {}, base_config(), NOW)
    assert result.allowed is True


def test_daily_cap_blocks_at_limit():
    result = gates.gate_daily_cap(PacemakerState(), {"messages_sent_today": 5}, base_config(), NOW)
    assert result.allowed is False


def test_daily_cap_allows_below_limit():
    result = gates.gate_daily_cap(PacemakerState(), {"messages_sent_today": 2}, base_config(), NOW)
    assert result.allowed is True


def test_fatigue_window_blocks_wraparound_midnight():
    late_night = datetime(2026, 7, 3, 23, 45, tzinfo=TZ)
    result = gates.gate_fatigue_window(PacemakerState(), {}, base_config(), late_night)
    assert result.allowed is False


def test_fatigue_window_blocks_early_morning():
    early = datetime(2026, 7, 3, 6, 0, tzinfo=TZ)
    result = gates.gate_fatigue_window(PacemakerState(), {}, base_config(), early)
    assert result.allowed is False


def test_fatigue_window_allows_daytime():
    result = gates.gate_fatigue_window(PacemakerState(), {}, base_config(), NOW)
    assert result.allowed is True


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


def test_run_gates_returns_all_results_no_short_circuit():
    context = {"active_session": True, "messages_sent_today": 5}
    results = gates.run_gates(PacemakerState(), context, base_config(), NOW)
    assert len(results) == 5
    names = {r.name for r in results if not r.allowed}
    assert "active-suspend" in names
    assert "daily-cap" in names
