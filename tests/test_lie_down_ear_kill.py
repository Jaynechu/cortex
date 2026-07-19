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
    """P17: lie_down(rotate=True) refuses while the OWN ear tail (ancestry
    chains back to the resident's claude pid) is alive — no claim consumed
    (still awake after), no rotate, the signed refusal text."""
    monkeypatch.setattr(lie_down, "_ear_tail_pids", lambda c: [4242])
    monkeypatch.setattr(lie_down, "_chains_to_ancestor",
                        lambda pid, ancestor: pid == 4242)
    import cortex.window as _window
    monkeypatch.setattr(_window, "find_claude_pid", lambda c: 1000)
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


def test_rotate_proceeds_when_only_orphan_tail_present(cfg, monkeypatch):
    """codex P2 fix: a live tail whose ancestry does NOT chain to the resident's
    claude pid (orphan predecessor reparented to launchd, or a foreign window's
    tail) must NEVER block rotate — only the residue sweep touches it."""
    import cortex.window as _window
    monkeypatch.setattr(lie_down, "_ear_tail_pids", lambda c: [9999])
    monkeypatch.setattr(_window, "find_claude_pid", lambda c: 1000)
    # orphan: ppid chain never reaches 1000
    monkeypatch.setattr(lie_down, "_chains_to_ancestor", lambda pid, ancestor: False)
    sweep_calls = []
    monkeypatch.setattr(lie_down, "_kill_ear_tails",
                        lambda c: sweep_calls.append(c) or 1)
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, rotate=True)
    assert r["rotated"] is True
    assert "skipped" not in r
    assert sweep_calls == [cfg]  # orphan reaped by the residue sweep, not blocked


def test_rotate_refuses_only_when_resident_pid_unresolved_is_treated_as_no_owner(
        cfg, monkeypatch):
    """When the resident's claude pid cannot be verified (find_claude_pid ->
    None), a live tail is never treated as owned -> rotate is not blocked on an
    unverifiable ownership claim."""
    import cortex.window as _window
    monkeypatch.setattr(lie_down, "_ear_tail_pids", lambda c: [4242])
    monkeypatch.setattr(_window, "find_claude_pid", lambda c: None)
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, rotate=True)
    assert r["rotated"] is True
    assert "skipped" not in r


def test_plain_lie_down_never_refuses_even_with_live_own_tail(cfg, monkeypatch):
    """Plain (non-rotate) sleep NEVER refuses — the ear must stay alive for
    normal sleep."""
    import cortex.window as _window
    monkeypatch.setattr(lie_down, "_ear_tail_pids", lambda c: [4242])
    monkeypatch.setattr(_window, "find_claude_pid", lambda c: 1000)
    monkeypatch.setattr(lie_down, "_chains_to_ancestor", lambda pid, ancestor: True)
    wake_state.set_awake(cfg, 1, None)
    r = lie_down.lie_down(cfg, next_wake_min=200, rotate=False)
    assert "skipped" not in r
    assert wake_state.is_awake(cfg) is False  # normal lie_down still completes


def test_chains_to_ancestor_walks_real_ppid_via_ps(cfg, monkeypatch):
    """Exercise the real _chains_to_ancestor/_ppid_of ps-walk mechanics: tail pid
    -> shell pid -> resident claude pid is 'own'; a chain that bottoms out at
    launchd/init (ppid 1) without ever reaching the resident is an orphan."""
    ppid_map = {5000: 4000, 4000: 1000, 1000: 1}  # tail -> shell -> claude -> launchd

    def fake_run(cmd, *a, **kw):
        pid = int(cmd[-1])
        parent = ppid_map.get(pid)
        stdout = str(parent) if parent is not None else ""
        return subprocess.CompletedProcess(cmd, 0 if parent is not None else 1,
                                           stdout=stdout, stderr="")
    monkeypatch.setattr(lie_down.subprocess, "run", fake_run)

    assert lie_down._chains_to_ancestor(5000, 1000) is True   # tail -> shell -> claude
    assert lie_down._chains_to_ancestor(5000, 9999) is False  # never reaches 9999
    assert lie_down._chains_to_ancestor(4000, 1000) is True   # one hop
    assert lie_down._chains_to_ancestor(1000, 1000) is True   # pid IS the ancestor


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
