from datetime import datetime, timedelta, timezone

from cortex.pacemaker import gates
from cortex.pacemaker.core import PacemakerState

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "gates": {
            "night": {"start": "23:00", "end": "06:00", "cap": 0},
            "daily_budget": {"tokens": 1_000_000},
        }
    }


# --- night mode (23-06, zero self-wakes; only schedule pierces) --------------

NIGHT_NOW = datetime(2026, 7, 4, 2, 0, tzinfo=TZ)  # inside 23:00-06:00
LATE_NOW = datetime(2026, 7, 4, 23, 30, tzinfo=TZ)  # inside, before midnight


def test_night_mode_allows_outside_window():
    result = gates.gate_night_mode(PacemakerState(), {"trigger_kinds": ["floor"]}, base_config(), NOW)
    assert result.allowed is True


def test_night_blocks_floor():
    result = gates.gate_night_mode(PacemakerState(), {"trigger_kinds": ["floor"]}, base_config(), NIGHT_NOW)
    assert result.allowed is False


def test_night_blocks_2330_floor():
    result = gates.gate_night_mode(PacemakerState(), {"trigger_kinds": ["floor"]}, base_config(), LATE_NOW)
    assert result.allowed is False


def test_night_blocks_self_scheduled_and_affect_flag():
    for kind in ("desire", "floor", "self_scheduled", "affect_flag"):
        result = gates.gate_night_mode(PacemakerState(), {"trigger_kinds": [kind]}, base_config(), NIGHT_NOW)
        assert result.allowed is False, kind


def test_night_schedule_pierces():
    result = gates.gate_night_mode(PacemakerState(), {"trigger_kinds": ["schedule"]}, base_config(), NIGHT_NOW)
    assert result.allowed is True


def test_rebirth_first_wake_after_0600_allowed():
    dawn = datetime(2026, 7, 4, 6, 1, tzinfo=TZ)  # window is [23:00, 06:00)
    result = gates.gate_night_mode(PacemakerState(), {"trigger_kinds": ["floor"]}, base_config(), dawn)
    assert result.allowed is True


def test_night_key_none_outside_window():
    assert gates.night_key(base_config(), NOW) is None


def test_night_key_present_inside_window():
    assert gates.night_key(base_config(), NIGHT_NOW) == "2026-07-03"


# --- daily budget ------------------------------------------------------------

def test_budget_allows_below_cap():
    ctx = {"trigger_kinds": ["floor"], "today_tokens": 500_000}
    assert gates.gate_daily_budget(PacemakerState(), ctx, base_config(), NOW).allowed is True


def test_budget_blocks_at_cap():
    ctx = {"trigger_kinds": ["floor"], "today_tokens": 1_000_000}
    assert gates.gate_daily_budget(PacemakerState(), ctx, base_config(), NOW).allowed is False


def test_budget_blocks_desire_self_affect_at_cap():
    for kind in ("floor", "desire", "self_scheduled", "affect_flag"):
        ctx = {"trigger_kinds": [kind], "today_tokens": 1_500_000}
        assert gates.gate_daily_budget(PacemakerState(), ctx, base_config(), NOW).allowed is False, kind


def test_budget_schedule_pierces_over_cap():
    ctx = {"trigger_kinds": ["schedule"], "today_tokens": 2_000_000}
    assert gates.gate_daily_budget(PacemakerState(), ctx, base_config(), NOW).allowed is True


def test_budget_disabled_when_zero():
    cfg = {"gates": {"daily_budget": {"tokens": 0}}}
    ctx = {"trigger_kinds": ["floor"], "today_tokens": 9_000_000}
    assert gates.gate_daily_budget(PacemakerState(), ctx, cfg, NOW).allowed is True


# --- run_gates ---------------------------------------------------------------

def test_run_gates_returns_both():
    ctx = {"trigger_kinds": ["floor"], "today_tokens": 0}
    results = gates.run_gates(PacemakerState(), ctx, base_config(), NIGHT_NOW)
    assert [r.name for r in results] == ["night-mode", "daily_budget"]
