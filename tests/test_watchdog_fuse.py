from __future__ import annotations

import pytest

from cortex import config, watchdog, wake_state
import cortex.lie_down as lie_down_mod


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "absent.toml")
    c["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    c["paths"]["handoff_file"] = str(tmp_path / "handoff.md")
    c.setdefault("wake", {}).setdefault("watchdog", {})["fuse_handoff_grace_sec"] = 1.0
    return c


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Fuse polls in a bounded wall-clock loop; skip the real sleeps so tests are
    # fast (the deadline still bounds iterations via time.time()).
    monkeypatch.setattr(watchdog.time, "sleep", lambda *_a, **_k: None)


def _stub_window(monkeypatch, calls):
    monkeypatch.setattr(watchdog.window, "send_esc", lambda cfg: calls.append("esc"))
    monkeypatch.setattr(watchdog.window, "inject_prompt",
                        lambda cfg, text: calls.append("prompt") or True)
    monkeypatch.setattr(watchdog, "_verify_esc_or_hard_interrupt",
                        lambda cfg, grace, trig: None)


def test_fuse_session_lies_down_itself_no_force(cfg, monkeypatch):
    calls = []
    _stub_window(monkeypatch, calls)
    forced = []
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda cfg, force_slept=None: forced.append(force_slept))
    wake_state.update(cfg, awake=True)

    real_load = wake_state.load
    seen = {"n": 0}

    def fake_load(c):
        seen["n"] += 1
        d = real_load(c)
        if seen["n"] >= 2:  # session lay down after the first poll
            d = dict(d)
            d.pop("awake", None)
        return d

    monkeypatch.setattr(watchdog.wake_state, "load", fake_load)
    watchdog._fuse(cfg, grace=0.0)
    assert "esc" in calls and "prompt" in calls
    assert forced == []  # session lay down itself -> no proxy lie_down


def test_fuse_timeout_no_handoff_forces_with_marker(cfg, monkeypatch):
    calls = []
    _stub_window(monkeypatch, calls)
    forced = []
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda cfg, force_slept=None: forced.append(force_slept))
    wake_state.update(cfg, awake=True)  # stays awake through grace
    watchdog._fuse(cfg, grace=0.0)
    assert len(forced) == 1
    assert forced[0] == "fuse"  # no handoff -> force_slept marker fires


def test_fuse_timeout_with_handoff_forces_without_marker(cfg, monkeypatch):
    calls = []
    _stub_window(monkeypatch, calls)
    forced = []

    def fake_inject(c, text):
        # Simulate the session writing its handoff (but never lying down).
        config.handoff_path(c).write_text("did stuff", encoding="utf-8")
        calls.append("prompt")
        return True

    monkeypatch.setattr(watchdog.window, "inject_prompt", fake_inject)
    monkeypatch.setattr(lie_down_mod, "lie_down",
                        lambda cfg, force_slept=None: forced.append(force_slept))
    wake_state.update(cfg, awake=True)
    watchdog._fuse(cfg, grace=0.0)
    assert len(forced) == 1
    assert forced[0] is None  # handoff written -> clean proxy, no catchup marker
