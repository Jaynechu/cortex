from datetime import datetime, timedelta, timezone

from cortex.pacemaker import gates
from cortex.pacemaker.core import PacemakerState

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "gates": {"daily_budget": {"tokens": 1_000_000}},
    }


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

def test_run_gates_returns_one():
    ctx = {"today_tokens": 0}
    results = gates.run_gates(PacemakerState(), ctx, base_config(), NOW)
    assert [r.name for r in results] == ["daily_budget"]
