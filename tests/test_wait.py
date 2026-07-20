from __future__ import annotations

import pytest

from cortex import config, wait, wake_state, watchdog


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    return c


def test_first_wait_ok_second_refused(cfg):
    # F5: no consecutive empty waits — one wait, then a second (pure) wait is
    # refused until activity restores the quota.
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")  # wait_spent=False
    r1 = wait.wait(cfg, 10)
    assert r1["ok"] is True
    assert wake_state.wait_spent(cfg) is True
    r2 = wait.wait(cfg, 10)
    assert r2["ok"] is False
    assert r2.get("refused") is True


def test_activity_restores_quota_then_wait_ok(cfg):
    # F5: a non-wait activity round restores the quota -> wait allowed again
    # ("play mode then wait again = allowed").
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    assert wait.wait(cfg, 10)["ok"] is True
    assert wait.wait(cfg, 10)["ok"] is False  # consecutive -> refused
    wake_state.restore_wait_quota(cfg)  # a tool call this round
    assert wait.wait(cfg, 10)["ok"] is True


def test_wait_spent_resets_on_new_wake(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 10)
    assert wake_state.wait_spent(cfg) is True
    # New wake -> flag reset, wait() allowed again.
    wake_state.set_awake(cfg, wake_log_id=2, transcript="t")
    assert wake_state.wait_spent(cfg) is False
    assert wait.wait(cfg, 10)["ok"] is True


def test_wait_spent_resets_on_lie_down(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 30)
    wake_state.clear_awake(cfg)  # lie_down clears the awake marker
    assert wake_state.wait_spent(cfg) is False


# --- auto observe consumes the round quota ------------------------------------

def _auto_observe(cfg):
    """Simulate the watchdog auto silence-gate arming an observe window: stamp
    tuck_pending, which spends the round's wait quota."""
    committed = wake_state.conditional_mutate(
        cfg, None, watchdog._stamp_tuck_pending())
    assert committed is True


def test_auto_observe_then_manual_wait_refused(cfg):
    # The watchdog auto-armed an observe window this round -> wait_spent set
    # -> a later manual wait() is refused (menu only).
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    _auto_observe(cfg)
    assert wake_state.wait_spent(cfg) is True
    r = wait.wait(cfg, 10)
    assert r["ok"] is False
    assert r.get("refused") is True


def test_no_auto_observe_manual_wait_ok_once(cfg):
    # No auto observe this round -> manual wait() works once; a second
    # consecutive manual wait is refused.
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    r1 = wait.wait(cfg, 10)
    assert r1["ok"] is True
    r2 = wait.wait(cfg, 10)
    assert r2["ok"] is False
    assert r2.get("refused") is True


# --- note (alarm-armed ack) ----------------------------------------------------

def test_wait_ok_returns_note_with_local_hm(cfg):
    # Success carries a "note" field ("alarm armed") so the model doesn't
    # misread the instant return as "the wait has already elapsed".
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    r = wait.wait(cfg, 10)
    assert r["ok"] is True
    assert "note" in r
    from datetime import datetime
    from zoneinfo import ZoneInfo
    until_dt = datetime.fromisoformat(r["until"])
    expected_hm = until_dt.astimezone(ZoneInfo(cfg["core"]["timezone"])).strftime("%H:%M")
    assert r["note"] == f"Alarm set {expected_hm}"


def test_wait_refused_has_no_note(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 10)
    r2 = wait.wait(cfg, 10)
    assert r2["ok"] is False
    assert "note" not in r2


def test_wait_note_uses_custom_template(cfg):
    cfg["wake"]["wait_ack_template"] = "⏰ {until_local}"
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    r = wait.wait(cfg, 5)
    assert r["note"].startswith("⏰ ")
