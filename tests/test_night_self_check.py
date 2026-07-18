"""P14 Fix 2 — pacemaker night self-check backstop.

Covers: in-window + all-channel-silent + asleep + no in-flight turn -> flag set;
awake -> no set (atomic guard); turn in flight (fresh transcript mtime) -> no set;
outside the night window -> no set; already-set -> no-op; the wake_state atomic
primitive holds awake==false + flag mutation in one strict-lock hold.
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cortex import config, pacemaker_tick, transcript, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    return c


def _tz(cfg):
    return ZoneInfo(cfg["core"]["timezone"])


def _at(cfg, hh, mm=0):
    return datetime(2026, 7, 19, hh, mm, tzinfo=_tz(cfg))


def _silent(monkeypatch, minutes):
    monkeypatch.setattr(transcript, "user_silent_min", lambda cfg: minutes)


def _mtime_idle(monkeypatch, cfg, idle_min):
    """Force transcript.mtime so raw idle = idle_min (None = no transcript)."""
    if idle_min is None:
        monkeypatch.setattr(transcript, "mtime", lambda cfg: None)
    else:
        monkeypatch.setattr(transcript, "mtime",
                             lambda cfg: time.time() - idle_min * 60.0)


# --- window helper ------------------------------------------------------------

def test_in_night_window_wraps_midnight(cfg):
    assert pacemaker_tick._in_night_window(_at(cfg, 23), cfg) is True
    assert pacemaker_tick._in_night_window(_at(cfg, 2), cfg) is True
    assert pacemaker_tick._in_night_window(_at(cfg, 5, 59), cfg) is True
    assert pacemaker_tick._in_night_window(_at(cfg, 6), cfg) is False
    assert pacemaker_tick._in_night_window(_at(cfg, 12), cfg) is False
    assert pacemaker_tick._in_night_window(_at(cfg, 21, 59), cfg) is False
    assert pacemaker_tick._in_night_window(_at(cfg, 22), cfg) is True


# --- self-check decision matrix ----------------------------------------------

def test_in_window_silent_asleep_sets_flag(cfg, monkeypatch):
    _silent(monkeypatch, 120)          # 2h >= 1.5h
    _mtime_idle(monkeypatch, cfg, 30)  # no in-flight turn
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert r is not None and "flag set" in r
    assert wake_state.is_night_mode(cfg) is True


def test_awake_no_set(cfg, monkeypatch):
    wake_state.set_awake(cfg, 1, None)  # a wake in progress
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, 30)
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert r is None
    assert wake_state.is_night_mode(cfg) is False


def test_turn_in_flight_no_set(cfg, monkeypatch):
    _silent(monkeypatch, 120)          # user-silence looks quiet...
    _mtime_idle(monkeypatch, cfg, 1)   # ...but transcript just written -> in flight
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert r is not None and "in flight" in r
    assert wake_state.is_night_mode(cfg) is False


def test_outside_window_no_set(cfg, monkeypatch):
    _silent(monkeypatch, 300)
    _mtime_idle(monkeypatch, cfg, 60)
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 12))  # midday
    assert r is None
    assert wake_state.is_night_mode(cfg) is False


def test_already_set_no_op(cfg, monkeypatch):
    wake_state.update(cfg, mode="night")
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, 30)
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert r is None  # early-out, no audit churn
    assert wake_state.is_night_mode(cfg) is True


def test_not_silent_enough_no_set(cfg, monkeypatch):
    _silent(monkeypatch, 30)           # 30min < 1.5h
    _mtime_idle(monkeypatch, cfg, 30)
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert r is None
    assert wake_state.is_night_mode(cfg) is False


def test_unknown_silence_holds(cfg, monkeypatch):
    _silent(monkeypatch, None)         # transcript unreadable -> hold
    _mtime_idle(monkeypatch, cfg, 30)
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert r is None
    assert wake_state.is_night_mode(cfg) is False


def test_no_transcript_mtime_still_sets(cfg, monkeypatch):
    """mtime unavailable -> no in-flight evidence, so the flag still sets when
    user-silence already cleared the bar."""
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, None)
    r = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert r is not None and "flag set" in r
    assert wake_state.is_night_mode(cfg) is True


# --- atomic primitive ---------------------------------------------------------

def test_try_set_night_mode_auto_sets_when_asleep(cfg):
    assert wake_state.try_set_night_mode_auto(cfg) is True
    assert wake_state.is_night_mode(cfg) is True


def test_try_set_night_mode_auto_noop_when_awake(cfg):
    wake_state.set_awake(cfg, 1, None)
    assert wake_state.try_set_night_mode_auto(cfg) is False
    assert wake_state.is_night_mode(cfg) is False


def test_try_set_night_mode_auto_noop_when_already_set(cfg):
    wake_state.update(cfg, mode="night")
    assert wake_state.try_set_night_mode_auto(cfg) is False
    assert wake_state.is_night_mode(cfg) is True
