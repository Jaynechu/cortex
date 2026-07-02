from datetime import datetime, timedelta, timezone

from cortex.pacemaker.expect_reply import ExpectReplyState, evaluate, next_check_at, start

TZ = timezone(timedelta(hours=10))
NOW = datetime(2026, 7, 3, 12, 0, tzinfo=TZ)


def base_config():
    return {
        "expect_reply": {
            "check_interval_min": 30,
            "worry_increment": 0.05,
            "tone_levels": ["neutral", "concerned", "worried", "anxious"],
        }
    }


def test_not_pending_no_op():
    state = ExpectReplyState()
    new_state, worry_delta, reason = evaluate(state, {}, base_config(), NOW)
    assert new_state == state
    assert worry_delta == 0.0
    assert reason is None


def test_replied_clears_pending():
    state = start(NOW)
    new_state, worry_delta, reason = evaluate(state, {"replied": True}, base_config(), NOW + timedelta(minutes=31))
    assert new_state.pending is False
    assert worry_delta == 0.0
    assert reason is None


def test_not_yet_due_no_op():
    state = start(NOW)
    new_state, worry_delta, reason = evaluate(state, {}, base_config(), NOW + timedelta(minutes=10))
    assert new_state == state
    assert worry_delta == 0.0
    assert reason is None


def test_first_silent_check_escalates_once():
    state = start(NOW)
    new_state, worry_delta, reason = evaluate(state, {}, base_config(), NOW + timedelta(minutes=30))
    assert new_state.checks_done == 1
    assert new_state.tone_level == 1
    assert worry_delta == 0.05
    assert reason is not None
    assert reason.kind == "expect_reply"


def test_repeated_silence_escalates_tone_and_caps():
    config = base_config()
    state = start(NOW)
    now = NOW
    tone_levels_len = len(config["expect_reply"]["tone_levels"])
    for i in range(10):
        now = now + timedelta(minutes=30)
        state, worry_delta, reason = evaluate(state, {}, config, now)
        assert worry_delta == 0.05
        assert reason is not None
    assert state.checks_done == 10
    assert state.tone_level == tone_levels_len - 1  # capped


def test_next_check_at_pending():
    state = start(NOW)
    assert next_check_at(state, base_config()) == NOW + timedelta(minutes=30)


def test_next_check_at_not_pending_is_none():
    assert next_check_at(ExpectReplyState(), base_config()) is None
