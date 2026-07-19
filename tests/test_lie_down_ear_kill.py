"""P16: a rotate lie_down physically kills the wake_signal ear tail (registration
already dropped by _mark_rotated), and the kill helper touches nothing else —
alarm sentinel / ledger / watchdog and every other wake_state key stay intact.
"""
from __future__ import annotations

import subprocess

import pytest

from cortex import config, lie_down, wake_state

# Grab the real helpers at import time — conftest's autouse _no_real_ear_kill
# stubs lie_down._ear_tail_pids/_kill_ear_tails to "no tail" for every test; the
# tests that exercise the real mechanics call these references directly instead.
_REAL_KILL = lie_down._kill_ear_tails
_REAL_EAR_TAIL_PIDS = lie_down._ear_tail_pids


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["wake"]["sentinel"] = False  # no real sentinel spawn in the suite
    return c


def test_rotate_invokes_ear_tail_kill(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(lie_down, "_kill_ear_tails", lambda c: calls.append(c) or 0)
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, rotate=True)
    assert r["rotated"] is True
    assert calls == [cfg]  # kill invoked exactly once, after the rotate landed


def test_plain_lie_down_does_not_kill_ear(cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(lie_down, "_kill_ear_tails", lambda c: calls.append(c) or 0)
    wake_state.set_awake(cfg, 1, None)
    lie_down.lie_down(cfg, next_wake_min=200, rotate=False)
    assert calls == []  # no rotate -> ear untouched


def test_kill_ear_tails_narrows_to_signal_log_and_skips_self(cfg, monkeypatch):
    signal_log = str(config.wake_signal_log_path(cfg))
    seen = {}

    def fake_run(cmd, *a, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="4242\n%d\n" % __import__("os").getpid(), stderr="")

    killed = []
    monkeypatch.setattr(lie_down, "_ear_tail_pids", _REAL_EAR_TAIL_PIDS)
    monkeypatch.setattr(lie_down.subprocess, "run", fake_run)
    monkeypatch.setattr(lie_down.os, "kill", lambda pid, sig: killed.append(pid))
    n = _REAL_KILL(cfg)
    # pgrep narrowed to the exact resolved signal-log path
    assert seen["cmd"][:2] == ["pgrep", "-f"]
    assert signal_log in seen["cmd"][2]
    # own pid skipped, only the foreign tail SIGTERMed
    assert killed == [4242]
    assert n == 1


def test_rotate_refuses_while_own_ear_tail_alive(cfg, monkeypatch):
    """P17: lie_down(rotate=True) refuses while the own ear tail is alive — no
    claim consumed (still awake after), no rotate, the signed refusal text."""
    monkeypatch.setattr(lie_down, "_ear_tail_pids", lambda c: [4242])
    kill_calls = []
    monkeypatch.setattr(lie_down, "_kill_ear_tails", lambda c: kill_calls.append(c) or 0)
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, rotate=True)
    assert r["skipped"] == "rotate_refused"
    assert r["rotated"] is False
    assert r["refused"] == cfg["wake"]["rotate_refuse_text"]
    assert kill_calls == []  # no rotate landed -> residue sweep never ran
    assert wake_state.is_awake(cfg) is True  # claim NOT consumed


def test_rotate_proceeds_once_ear_tail_dead(cfg, monkeypatch):
    """Second call (ear tail already stopped) passes through normally."""
    monkeypatch.setattr(lie_down, "_ear_tail_pids", lambda c: [])
    monkeypatch.setattr(lie_down, "_kill_ear_tails", lambda c: 0)
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, rotate=True)
    assert r["rotated"] is True
    assert "skipped" not in r


def test_plain_lie_down_never_refuses_even_with_live_tail(cfg, monkeypatch):
    """Plain (non-rotate) sleep NEVER refuses — the ear must stay alive for
    normal sleep."""
    monkeypatch.setattr(lie_down, "_ear_tail_pids", lambda c: [4242])
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, rotate=False)
    assert "skipped" not in r
    assert wake_state.is_awake(cfg) is False  # normal lie_down still completes


def test_kill_ear_tails_leaves_alarm_and_wake_state_untouched(cfg, monkeypatch):
    """The kill helper only signals processes — it must not mutate wake_state
    (sentinel pid, ledger, night flag, registration all preserved)."""
    wake_state.update(cfg, sentinel_pid=999, next_wake_at="2099-01-01T00:00:00+00:00",
                      mode="night", cortex_claude_sid="keep-me")
    before = wake_state.load(cfg)
    monkeypatch.setattr(lie_down, "_ear_tail_pids", _REAL_EAR_TAIL_PIDS)
    monkeypatch.setattr(
        lie_down.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0] if a else [], 1, stdout="", stderr=""))
    _REAL_KILL(cfg)
    after = wake_state.load(cfg)
    assert after.get("sentinel_pid") == 999
    assert after.get("next_wake_at") == "2099-01-01T00:00:00+00:00"
    assert after.get("mode") == "night"
    assert after.get("cortex_claude_sid") == "keep-me"
    assert before == after
