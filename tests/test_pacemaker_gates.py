from datetime import datetime, timedelta, timezone

from cortex.pacemaker import gates
from cortex.pacemaker.core import PacemakerState

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "night": {"cap": 3},
        "gates": {"daily_budget": {"tokens": 1_000_000}},
    }


# --- night cap (flag-based) --------------------------------------------------

def test_night_cap_allows_when_flag_absent():
    ctx = {"mode": None, "today_tokens": 0}
    assert gates.gate_night_cap(PacemakerState(), ctx, base_config(), NOW).allowed is True


def test_night_cap_allows_under_cap():
    ctx = {"mode": "night"}
    state = PacemakerState(night_cap_key="night", night_wake_count=1)
    assert gates.gate_night_cap(state, ctx, base_config(), NOW).allowed is True


def test_night_cap_blocks_at_cap():
    ctx = {"mode": "night"}
    state = PacemakerState(night_cap_key="night", night_wake_count=3)
    r = gates.gate_night_cap(state, ctx, base_config(), NOW)
    assert r.allowed is False
    assert "night cap reached" in r.reason


def test_night_cap_ignores_count_when_day():
    # Day (no flag): even a high stale count never blocks.
    ctx = {"mode": None}
    state = PacemakerState(night_cap_key="night", night_wake_count=99)
    assert gates.gate_night_cap(state, ctx, base_config(), NOW).allowed is True


# --- daily budget ------------------------------------------------------------

def test_budget_allows_below_cap():
    ctx = {"today_tokens": 500_000}
    assert gates.gate_daily_budget(PacemakerState(), ctx, base_config(), NOW).allowed is True


def test_budget_blocks_at_cap():
    ctx = {"today_tokens": 1_000_000}
    assert gates.gate_daily_budget(PacemakerState(), ctx, base_config(), NOW).allowed is False


def test_budget_disabled_when_zero():
    cfg = {"gates": {"daily_budget": {"tokens": 0}}}
    ctx = {"today_tokens": 9_000_000}
    assert gates.gate_daily_budget(PacemakerState(), ctx, cfg, NOW).allowed is True


# --- run_gates ---------------------------------------------------------------

def test_run_gates_returns_both():
    ctx = {"mode": None, "today_tokens": 0}
    results = gates.run_gates(PacemakerState(), ctx, base_config(), NOW)
    assert [r.name for r in results] == ["night-cap", "daily_budget"]
