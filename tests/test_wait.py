from __future__ import annotations

import pytest

from cortex import config, wait, wake_state, watchdog


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    return c


def test_first_wait_ok_second_refused(cfg):
    # wait_max_per_wake = 1 (P7): one wait per wake, then the menu only.
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")  # resets wait_count
    r1 = wait.wait(cfg, 10)
    assert r1["ok"] is True
    assert r1["wait_count"] == 1
    assert r1["cap"] == 1
    r2 = wait.wait(cfg, 10)
    assert r2["ok"] is False
    assert r2.get("refused") is True
    assert r2["wait_count"] == 1  # not incremented past the cap


def test_wait_uncapped_when_cap_zero(cfg):
    # cap 0 = uncapped escape hatch (opt-in): wait loops forever.
    cfg["wake"]["wait_max_per_wake"] = 0
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    for _ in range(5):
        r = wait.wait(cfg, 10)
        assert r["ok"] is True
    assert r["cap"] == 0
    assert wake_state.get_wait_count(cfg) == 5


def test_wait_count_resets_on_new_wake(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 10)
    assert wake_state.get_wait_count(cfg) == 1
    # New wake -> counter reset, wait() allowed again.
    wake_state.set_awake(cfg, wake_log_id=2, transcript="t")
    assert wake_state.get_wait_count(cfg) == 0
    assert wait.wait(cfg, 10)["ok"] is True


def test_wait_count_resets_on_lie_down(cfg):
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    wait.wait(cfg, 30)
    wake_state.clear_awake(cfg)  # lie_down clears the awake marker
    assert wake_state.get_wait_count(cfg) == 0


# --- auto + manual share ONE quota per wake (whichever fires first) ----------

def _auto_observe(cfg):
    """Simulate the watchdog auto silence-gate arming an observe window: stamp
    tuck_pending, which consumes the shared wait quota."""
    committed = wake_state.conditional_mutate(
        cfg, None, watchdog._stamp_tuck_pending())
    assert committed is True


def test_auto_observe_then_manual_wait_refused(cfg):
    # The watchdog auto-armed an observe window this wake -> wait_count consumed
    # -> a later manual wait() is refused (menu only).
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    _auto_observe(cfg)
    assert wake_state.get_wait_count(cfg) == 1
    r = wait.wait(cfg, 10)
    assert r["ok"] is False
    assert r.get("refused") is True


def test_no_auto_observe_manual_wait_ok_once(cfg):
    # No auto observe this wake (user absent path never fired) -> manual wait()
    # works, once; a second manual wait is refused.
    wake_state.set_awake(cfg, wake_log_id=1, transcript="t")
    r1 = wait.wait(cfg, 10)
    assert r1["ok"] is True
    assert r1["wait_count"] == 1
    r2 = wait.wait(cfg, 10)
    assert r2["ok"] is False
    assert r2.get("refused") is True
