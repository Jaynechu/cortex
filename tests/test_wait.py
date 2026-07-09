from __future__ import annotations

import pytest

from cortex import config, wait, wake_state


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    return c


def test_wait_within_cap_returns_ok(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")  # resets wait_count
    r1 = wait.wait(cfg, 30)
    assert r1["ok"] is True
    assert r1["wait_count"] == 1
    r2 = wait.wait(cfg, 30)
    assert r2["ok"] is True
    assert r2["wait_count"] == 2


def test_wait_third_call_refused(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 30)
    wait.wait(cfg, 30)
    r3 = wait.wait(cfg, 30)
    assert r3["ok"] is False
    assert r3["refused"] is True
    assert r3["cap"] == 2
    # A refused call does not extend the silence window or bump the counter.
    assert wake_state.get_wait_count(cfg) == 2


def test_wait_count_resets_on_new_wake(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 30)
    wait.wait(cfg, 30)
    assert wake_state.get_wait_count(cfg) == 2
    # New wake -> counter reset, wait() allowed again.
    wake_state.set_awake(cfg, wake_log_id=2, transcript="t")
    assert wake_state.get_wait_count(cfg) == 0
    assert wait.wait(cfg, 30)["ok"] is True


def test_wait_count_resets_on_lie_down(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 30)
    wake_state.clear_awake(cfg)  # lie_down clears the awake marker
    assert wake_state.get_wait_count(cfg) == 0
