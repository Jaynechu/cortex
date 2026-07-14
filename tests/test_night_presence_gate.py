"""Phase 3 D9: NIGHT close is non-urgent -> presence-gated. When the user
messaged within silent_max_min, the wrap-up injection HOLDS (without consuming
the once-per-night dedup key) so a later tick delivers it once chat is quiet.
No iTerm/claude here — window.inject_prompt + user_silent_min are stubbed."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cortex import config, pacemaker_tick, transcript, wake_state
from cortex import window


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    # Night window: 23:00-06:00 so a 23:30 `now` is inside it.
    c["gates"]["night"]["start"] = "23:00"
    c["gates"]["night"]["end"] = "06:00"
    return c


def _night_now(cfg):
    tz = ZoneInfo(cfg["core"]["timezone"])
    return datetime(2026, 7, 14, 23, 30, tzinfo=tz)


def _spy_inject(monkeypatch):
    calls = []
    monkeypatch.setattr(window, "inject_prompt",
                        lambda c, text: calls.append(text) or True)
    return calls


def test_night_holds_when_user_present(cfg, monkeypatch):
    wake_state.set_awake(cfg, 1, None)
    calls = _spy_inject(monkeypatch)
    monkeypatch.setattr(transcript, "user_silent_min", lambda c: 3.0)  # < 15
    msg = pacemaker_tick._night_close(cfg, _night_now(cfg), wake_state.load(cfg))
    assert msg is not None and "hold" in msg
    assert calls == []  # no injection while the user is present
    # dedup key NOT consumed -> a later tick can still deliver it
    assert wake_state.load(cfg).get("night_wrap_key") is None


def test_night_fires_when_user_quiet(cfg, monkeypatch):
    wake_state.set_awake(cfg, 1, None)
    calls = _spy_inject(monkeypatch)
    monkeypatch.setattr(transcript, "user_silent_min", lambda c: 20.0)  # >= 15
    msg = pacemaker_tick._night_close(cfg, _night_now(cfg), wake_state.load(cfg))
    assert msg == "night close: wrap-up injected"
    assert len(calls) == 1 and "[NIGHT]" in calls[0]
    # dedup key set -> the same night never double-injects
    key = wake_state.load(cfg).get("night_wrap_key")
    assert key is not None
    again = pacemaker_tick._night_close(cfg, _night_now(cfg), wake_state.load(cfg))
    assert again is None


def test_night_holds_when_epoch_moves(cfg, monkeypatch):
    """FIX 2 (D9/trap 3): a user reset / lie_down landing between the epoch
    snapshot and the inject must cancel the NIGHT nudge -> hold, key un-consumed,
    no injection."""
    wake_state.set_awake(cfg, 1, None)
    calls = _spy_inject(monkeypatch)
    monkeypatch.setattr(transcript, "user_silent_min", lambda c: 20.0)  # quiet

    # Snapshot the real token, then bump gen to simulate a superseding event that
    # lands after the presence check but before the inject.
    real_current_epoch = wake_state.current_epoch

    def capture_then_bump(c):
        tok = real_current_epoch(c)
        wake_state.bump_gen(c)  # a user message / lie_down after the snapshot
        return tok

    monkeypatch.setattr(wake_state, "current_epoch", capture_then_bump)
    msg = pacemaker_tick._night_close(cfg, _night_now(cfg), wake_state.load(cfg))
    assert msg == "night close: epoch moved -> hold"
    assert calls == []  # nothing injected
    assert wake_state.load(cfg).get("night_wrap_key") is None  # key un-consumed


def test_night_fires_when_silence_unknown(cfg, monkeypatch):
    """None silence signal (no user turn in tail / no transcript) must NOT block
    the wrap-up — a missing signal is not presence."""
    wake_state.set_awake(cfg, 1, None)
    calls = _spy_inject(monkeypatch)
    monkeypatch.setattr(transcript, "user_silent_min", lambda c: None)
    msg = pacemaker_tick._night_close(cfg, _night_now(cfg), wake_state.load(cfg))
    assert msg == "night close: wrap-up injected"
    assert len(calls) == 1
