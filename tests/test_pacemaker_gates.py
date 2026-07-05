from datetime import datetime, timedelta, timezone

from cortex.pacemaker import gates
from cortex.pacemaker.core import PacemakerState

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "gates": {
            "night": {"start": "00:00", "end": "06:00", "cap": 1},
        }
    }


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


# --- run_gates ---------------------------------------------------------------

def test_run_gates_returns_night_mode_only():
    # Night mode is the sole gate; spend protection lives elsewhere.
    context = {"trigger_kinds": ["floor"]}
    results = gates.run_gates(PacemakerState(), context, base_config(), NIGHT_NOW)
    assert [r.name for r in results] == ["night-mode"]
