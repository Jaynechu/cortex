from datetime import datetime, timedelta, timezone

from cortex.pacemaker.desire import DesireState, tick

TZ = timezone(timedelta(hours=10))


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
            "worry": {"base_rate_per_min": 0.002, "decay_rate_per_min": 0.001},
            "duty": {"base_rate_per_min": 0.003, "decay_rate_per_min": 0.0005},
        }
    }


def test_first_tick_zero_elapsed_no_change():
    state = DesireState(attachment=0.5)
    now = datetime(2026, 7, 3, 10, 0, tzinfo=TZ)
    new_state = tick(state, {}, base_config(), now)
    assert new_state.attachment == 0.5
    assert new_state.last_tick_at == now


def test_attachment_busy_suppresses_accrual():
    state = DesireState(attachment=0.5, last_tick_at=datetime(2026, 7, 3, 10, 0, tzinfo=TZ))
    now = state.last_tick_at + timedelta(minutes=30)
    context = {"cal_busy": True, "at_home": False}
    new_state = tick(state, context, base_config(), now)
    # busy_multiplier=0 -> only decay applies, value should drop or stay same
    assert new_state.attachment <= 0.5


def test_attachment_home_free_long_gap_fast_accrual():
    state = DesireState(attachment=0.5, last_tick_at=datetime(2026, 7, 3, 10, 0, tzinfo=TZ))
    now = state.last_tick_at + timedelta(minutes=30)
    context = {
        "cal_busy": False,
        "at_home": True,
        "last_real_chat_at": now - timedelta(minutes=120),
    }
    fast_state = tick(state, context, base_config(), now)

    neutral_context = {
        "cal_busy": False,
        "at_home": False,
        "last_real_chat_at": now - timedelta(minutes=120),
    }
    neutral_state = tick(state, neutral_context, base_config(), now)

    assert fast_state.attachment > neutral_state.attachment


def test_attachment_home_but_gap_not_long_enough_uses_neutral_rate():
    state = DesireState(attachment=0.5, last_tick_at=datetime(2026, 7, 3, 10, 0, tzinfo=TZ))
    now = state.last_tick_at + timedelta(minutes=30)
    context = {
        "cal_busy": False,
        "at_home": True,
        "last_real_chat_at": now - timedelta(minutes=5),  # below gap_threshold_min=60
    }
    result = tick(state, context, base_config(), now)

    neutral_context = {"cal_busy": False, "at_home": False, "last_real_chat_at": None}
    neutral_result = tick(state, neutral_context, base_config(), now)

    assert result.attachment == neutral_result.attachment


def test_clamp_upper_bound():
    state = DesireState(attachment=0.999, last_tick_at=datetime(2026, 7, 3, 10, 0, tzinfo=TZ))
    now = state.last_tick_at + timedelta(hours=100)
    context = {"cal_busy": False, "at_home": True, "last_real_chat_at": None}
    result = tick(state, context, base_config(), now)
    assert result.attachment == 1.0


def test_clamp_lower_bound():
    state = DesireState(worry=0.001, last_tick_at=datetime(2026, 7, 3, 10, 0, tzinfo=TZ))
    now = state.last_tick_at + timedelta(hours=100)
    config = base_config()
    config["desire"]["worry"] = {"base_rate_per_min": 0.0, "decay_rate_per_min": 0.5}
    result = tick(state, {}, config, now)
    assert result.worry == 0.0


def test_other_dimensions_not_context_modulated():
    state = DesireState(curiosity=0.5, last_tick_at=datetime(2026, 7, 3, 10, 0, tzinfo=TZ))
    now = state.last_tick_at + timedelta(minutes=30)
    busy_result = tick(state, {"cal_busy": True}, base_config(), now)
    free_result = tick(state, {"cal_busy": False, "at_home": True}, base_config(), now)
    assert busy_result.curiosity == free_result.curiosity
