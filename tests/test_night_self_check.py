"""P14 Fix 2 — pacemaker night two-stage bell-ringer.

Stage 1: in-window + all-channel-silent + asleep + no in-flight turn + not yet
kicked -> mark night_kick once + send the night_due wake bell (short-circuit).
Stage 2: bell already fired, flag still unset, still asleep -> hard fallback sets
night flag + rotate marker atomically. Awake / in-flight / outside window / not
silent enough / already-set -> no action.
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


def _no_bell(monkeypatch):
    """Record night_due kicks without spawning a real tick."""
    calls = []
    from cortex import kick as kick_mod
    monkeypatch.setattr(kick_mod, "kick",
                        lambda cfg, kind, **f: calls.append((kind, f)) or {"ok": True})
    return calls


# --- window helper ------------------------------------------------------------

def test_in_night_window_wraps_midnight(cfg):
    assert pacemaker_tick._in_night_window(_at(cfg, 23), cfg) is True
    assert pacemaker_tick._in_night_window(_at(cfg, 2), cfg) is True
    assert pacemaker_tick._in_night_window(_at(cfg, 5, 59), cfg) is True
    assert pacemaker_tick._in_night_window(_at(cfg, 6), cfg) is False
    assert pacemaker_tick._in_night_window(_at(cfg, 12), cfg) is False
    assert pacemaker_tick._in_night_window(_at(cfg, 21, 59), cfg) is False
    assert pacemaker_tick._in_night_window(_at(cfg, 22), cfg) is True


# --- Stage 1: bell ------------------------------------------------------------

def test_stage1_sends_bell_once(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    _silent(monkeypatch, 120)          # 2h >= 1.5h
    _mtime_idle(monkeypatch, cfg, 30)  # no in-flight turn
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert short is True and msg is not None and "bell sent" in msg
    assert len(calls) == 1
    kind, fields = calls[0]
    assert kind == "night_due"
    assert "2.0h silent" in fields.get("text", "")   # {silent_h} rendered
    assert wake_state.load(cfg).get("night_kick") is True
    assert wake_state.is_night_mode(cfg) is False     # flag NOT set by Stage 1


def test_stage1_no_duplicate_bell(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, 30)
    pacemaker_tick._night_self_check(cfg, _at(cfg, 23))   # Stage 1
    assert len(calls) == 1
    # second tick: marker set + flag still unset -> Stage 2, no more bell
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23, 30))
    assert short is False
    assert len(calls) == 1                                # bell not re-sent


# --- Stage 2: hard fallback ---------------------------------------------------

def test_stage2_fallback_sets_flag_and_rotate(cfg, monkeypatch):
    _no_bell(monkeypatch)
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, 30)
    pacemaker_tick._night_self_check(cfg, _at(cfg, 23))       # Stage 1 (bell)
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 0, 30))  # Stage 2
    assert short is False and msg is not None and "fallback" in msg
    d = wake_state.load(cfg)
    assert d.get("mode") == "night"
    assert d.get("rotated") is True


# --- no-action matrix ---------------------------------------------------------

def test_awake_no_action(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    wake_state.set_awake(cfg, 1, None)  # a wake in progress
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, 30)
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert short is False and len(calls) == 0
    assert wake_state.is_night_mode(cfg) is False
    assert wake_state.load(cfg).get("night_kick") is None


def test_turn_in_flight_no_action(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    _silent(monkeypatch, 120)          # user-silence looks quiet...
    _mtime_idle(monkeypatch, cfg, 1)   # ...but transcript just written -> in flight
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert short is False and msg is not None and "in flight" in msg
    assert len(calls) == 0
    assert wake_state.load(cfg).get("night_kick") is None


def test_outside_window_no_action(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    _silent(monkeypatch, 300)
    _mtime_idle(monkeypatch, cfg, 60)
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 12))  # midday
    assert (msg, short) == (None, False) and len(calls) == 0


def test_already_night_flag_no_op(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    wake_state.update(cfg, mode="night")
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, 30)
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert (msg, short) == (None, False) and len(calls) == 0


def test_not_silent_enough_no_action(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    _silent(monkeypatch, 30)           # 30min < 1.5h
    _mtime_idle(monkeypatch, cfg, 30)
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert (msg, short) == (None, False) and len(calls) == 0


def test_unknown_silence_holds(cfg, monkeypatch):
    calls = _no_bell(monkeypatch)
    _silent(monkeypatch, None)         # transcript unreadable -> hold
    _mtime_idle(monkeypatch, cfg, 30)
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert (msg, short) == (None, False) and len(calls) == 0


def test_no_transcript_mtime_still_bells(cfg, monkeypatch):
    """mtime unavailable -> no in-flight evidence, so Stage 1 still bells."""
    calls = _no_bell(monkeypatch)
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, None)
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23))
    assert short is True and len(calls) == 1


# --- atomic primitives --------------------------------------------------------

def test_try_mark_night_kick_once(cfg):
    assert wake_state.try_mark_night_kick(cfg) is True
    assert wake_state.try_mark_night_kick(cfg) is False   # already marked
    assert wake_state.load(cfg).get("night_kick") is True


def test_try_mark_night_kick_noop_when_awake(cfg):
    wake_state.set_awake(cfg, 1, None)
    assert wake_state.try_mark_night_kick(cfg) is False
    assert wake_state.load(cfg).get("night_kick") is None


def test_try_mark_night_kick_noop_when_flag_set(cfg):
    wake_state.update(cfg, mode="night")
    assert wake_state.try_mark_night_kick(cfg) is False


def test_try_set_night_fallback_sets_flag_and_rotate(cfg):
    assert wake_state.try_set_night_fallback(cfg) is True
    d = wake_state.load(cfg)
    assert d.get("mode") == "night" and d.get("rotated") is True


def test_try_set_night_fallback_noop_when_awake(cfg):
    wake_state.set_awake(cfg, 1, None)
    assert wake_state.try_set_night_fallback(cfg) is False
    assert wake_state.is_night_mode(cfg) is False


def test_try_set_night_fallback_noop_when_already_set(cfg):
    wake_state.update(cfg, mode="night")
    assert wake_state.try_set_night_fallback(cfg) is False


def test_clear_night_mode_clears_kick_marker(cfg):
    wake_state.update(cfg, mode="night", night_kick=True)
    assert wake_state.clear_night_mode(cfg) is True
    d = wake_state.load(cfg)
    assert d.get("mode") is None and d.get("night_kick") is None
