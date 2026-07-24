"""wake_state atomicity + lock tests: _save is atomic (temp + os.replace) and the
sibling .lock exists. Also lie_down --next-wake-min is required at the CLI."""
from __future__ import annotations

import pytest

from cortex import config, lie_down, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    return c


def test_save_is_atomic_no_tmp_left(cfg):
    wake_state.update(cfg, awake=True)
    p = wake_state.wake_state_path(cfg)
    assert p.exists()
    # No stray temp files from the atomic replace.
    leftovers = list(p.parent.glob("*.tmp.*"))
    assert leftovers == []


def test_lock_file_path_is_sibling(cfg):
    lp = wake_state.lock_path(cfg)
    assert lp == wake_state.wake_state_path(cfg).with_suffix(".lock")


# --- night flag lifecycle (P8) -----------------------------------------------

def test_night_flag_survives_wake_cycle(cfg):
    """The night flag persists across set_awake / clear_awake (it is NOT an
    awake-key), so it outlives individual wakes until the morning clear."""
    wake_state.update(cfg, mode="night")
    wake_state.set_awake(cfg, 1, None)  # a wake begins
    assert wake_state.is_night_mode(cfg) is True
    wake_state.clear_awake(cfg)         # the wake ends
    assert wake_state.is_night_mode(cfg) is True  # flag still set


def test_clear_night_mode_returns_true_once(cfg):
    wake_state.update(cfg, mode="night")
    assert wake_state.clear_night_mode(cfg) is True
    assert wake_state.is_night_mode(cfg) is False
    assert wake_state.clear_night_mode(cfg) is False  # no-op second call


def test_lie_down_night_mode_sets_flag_under_lock(cfg):
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, mode="night")
    assert r["mode"] == "night"
    assert wake_state.is_night_mode(cfg) is True


def test_lie_down_night_mode_via_cli(cfg, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: cfg)
    wake_state.set_awake(cfg, 1, None)
    rc = lie_down.main(["--next-wake-min", "150", "--mode", "night"])
    assert rc == 0
    assert wake_state.is_night_mode(cfg) is True


def test_mark_kick_round_once_then_take(cfg):
    """External-wake carrier primitive: marks only while awake, idempotent (a
    second mark before consumption is a no-op), and take_kick_round consumes it
    exactly once."""
    wake_state.set_awake(cfg, 1, None)
    assert wake_state.mark_kick_round(cfg) is True
    assert wake_state.mark_kick_round(cfg) is False  # already pending -> no-op
    assert wake_state.peek_kick_round(cfg) is True
    assert wake_state.take_kick_round(cfg) is True
    assert wake_state.peek_kick_round(cfg) is False
    assert wake_state.take_kick_round(cfg) is False  # already consumed


def test_mark_kick_round_noop_when_asleep(cfg):
    wake_state.update(cfg, awake=None)
    assert wake_state.mark_kick_round(cfg) is False
    assert wake_state.peek_kick_round(cfg) is False


def test_sentinel_pid_self_guarded_clear(cfg):
    wake_state.set_sentinel_pid(cfg, 500)
    # Clearing with a mismatched pid is a no-op (a newer arm owns the record).
    wake_state.clear_sentinel_pid(cfg, only_if_pid=999)
    assert wake_state.get_sentinel_pid(cfg) == 500
    # Matching pid clears it.
    wake_state.clear_sentinel_pid(cfg, only_if_pid=500)
    assert wake_state.get_sentinel_pid(cfg) is None


def test_lie_down_cli_requires_next_wake_min(cfg, monkeypatch):
    monkeypatch.setenv("CORTEX_CONFIG", "/no/such/file.toml")
    # argparse required=True -> missing --next-wake-min exits non-zero.
    with pytest.raises(SystemExit) as exc:
        lie_down.main([])
    assert exc.value.code != 0
