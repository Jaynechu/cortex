"""P17 night: pacemaker night bell-ringer — two facts only (all-channel silence
+ the bell), NO forced teardown.

In-window + all-channel-silent + asleep + no in-flight turn + not yet kicked ->
mark night_kick once + send the night_due wake bell (short-circuit). Bell already
fired / awake / in-flight / outside window / not silent enough / already-set ->
no action. There is no Stage-2 hard fallback: if the window never acts on the
bell, nothing forces it (dead window handled at next due by ghost-handoff).
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
    monkeypatch.setattr(transcript, "global_user_silent_min", lambda cfg: minutes)


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


# --- the bell -----------------------------------------------------------------

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
    pacemaker_tick._night_self_check(cfg, _at(cfg, 23))   # bell
    assert len(calls) == 1
    # second tick: marker set -> early return, no bell, no forced teardown
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 23, 30))
    assert short is False
    assert len(calls) == 1                                # bell not re-sent


def test_bell_fired_never_forces_teardown(cfg, monkeypatch):
    """After the bell, a later still-asleep tick must NOT set the night flag or
    rotate marker — the Stage-2 hard fallback is deleted (P17)."""
    _no_bell(monkeypatch)
    _silent(monkeypatch, 120)
    _mtime_idle(monkeypatch, cfg, 30)
    pacemaker_tick._night_self_check(cfg, _at(cfg, 23))           # bell
    msg, short = pacemaker_tick._night_self_check(cfg, _at(cfg, 0, 30))
    assert (msg, short) == (None, False)
    d = wake_state.load(cfg)
    assert d.get("mode") is None
    assert d.get("rotated") is None


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


def test_clear_night_mode_clears_kick_marker(cfg):
    wake_state.update(cfg, mode="night", night_kick=True)
    assert wake_state.clear_night_mode(cfg) is True
    d = wake_state.load(cfg)
    assert d.get("mode") is None and d.get("night_kick") is None


# --- regression: the hard fallback is fully gone ------------------------------

def test_no_night_fallback_symbols_in_source():
    """The Stage-2 hard fallback (night_auto_fallback / try_set_night_fallback)
    is deleted repo-wide: no forged rotate marker can ever be set by the night
    self-check again (P17)."""
    import pathlib
    src = pathlib.Path(pacemaker_tick.__file__).resolve().parent
    for p in src.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "night_auto_fallback" not in text, p
        assert "try_set_night_fallback" not in text, p
