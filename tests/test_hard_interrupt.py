"""Process-level hard interrupt (SIGINT fallback for esc): pid discovery
(window.find_claude_pid / hard_interrupt) and the watchdog grace-window
wiring (watchdog._verify_esc_or_hard_interrupt). No real osascript/processes —
subprocess.run and window.wake_state are mocked."""
from __future__ import annotations

import subprocess

import pytest

from cortex import config, watchdog, window


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


class _FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


# --- pid discovery: (a) iTerm tty path -----------------------------------------

def test_find_claude_pid_via_tty(cfg, monkeypatch):
    monkeypatch.setattr(window.wake_state, "get_session_id", lambda c: "SID-1")
    monkeypatch.setattr(window, "_session_tty", lambda sid: "/dev/ttys003")

    def fake_run(cmd, **kw):
        assert cmd[:2] == ["ps", "-t"]
        assert cmd[2] == "ttys003"
        return _FakeCompleted(stdout="98145 claude\n98161 node\n")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert window.find_claude_pid(cfg) == 98145


def test_find_claude_pid_tty_ambiguous_falls_to_pgrep_skip(cfg, monkeypatch):
    """Two claude procs on the resolved tty -> tty path is ambiguous; pgrep
    fallback then finds nothing matching cortex_home -> overall None."""
    monkeypatch.setattr(window.wake_state, "get_session_id", lambda c: "SID-1")
    monkeypatch.setattr(window, "_session_tty", lambda sid: "/dev/ttys003")

    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        if cmd[0] == "ps":
            return _FakeCompleted(stdout="111 claude\n222 claude\n")
        if cmd[0] == "pgrep":
            return _FakeCompleted(stdout="", returncode=1)
        return _FakeCompleted()
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert window.find_claude_pid(cfg) is None


# --- pid discovery: (b) pgrep + cwd fallback -----------------------------------

def test_find_claude_pid_pgrep_fallback_matches_cwd(cfg, monkeypatch):
    monkeypatch.setattr(window.wake_state, "get_session_id", lambda c: None)  # no session -> skip tty

    def fake_run(cmd, **kw):
        if cmd[0] == "pgrep":
            return _FakeCompleted(stdout="501\n502\n")
        if cmd[0] == "lsof":
            pid = cmd[cmd.index("-p") + 1]
            home = str(config.cortex_home(cfg))
            out = f"n{home}\n" if pid == "501" else "n/some/other/dir\n"
            return _FakeCompleted(stdout=out)
        raise AssertionError(f"unexpected cmd {cmd}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert window.find_claude_pid(cfg) == 501


def test_find_claude_pid_pgrep_fallback_ambiguous_skips(cfg, monkeypatch):
    monkeypatch.setattr(window.wake_state, "get_session_id", lambda c: None)
    home = str(config.cortex_home(cfg))

    def fake_run(cmd, **kw):
        if cmd[0] == "pgrep":
            return _FakeCompleted(stdout="501\n502\n")
        if cmd[0] == "lsof":
            return _FakeCompleted(stdout=f"n{home}\n")  # both match -> ambiguous
        raise AssertionError(f"unexpected cmd {cmd}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert window.find_claude_pid(cfg) is None


def test_find_claude_pid_no_candidates(cfg, monkeypatch):
    monkeypatch.setattr(window.wake_state, "get_session_id", lambda c: None)

    def fake_run(cmd, **kw):
        if cmd[0] == "pgrep":
            return _FakeCompleted(stdout="", returncode=1)
        raise AssertionError(f"unexpected cmd {cmd}")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert window.find_claude_pid(cfg) is None


# --- hard_interrupt: SIGINT, never SIGKILL, ambiguous -> skip -----------------

def test_hard_interrupt_sends_sigint(cfg, monkeypatch):
    import signal
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 4242)
    sent = {}
    monkeypatch.setattr(window.os, "kill", lambda pid, sig: sent.update(pid=pid, sig=sig))

    assert window.hard_interrupt(cfg) == 4242
    assert sent == {"pid": 4242, "sig": signal.SIGINT}


def test_hard_interrupt_ambiguous_pid_skips(cfg, monkeypatch):
    monkeypatch.setattr(window, "find_claude_pid", lambda c: None)
    called = []
    monkeypatch.setattr(window.os, "kill", lambda pid, sig: called.append(pid))

    assert window.hard_interrupt(cfg) is None
    assert called == []


def test_hard_interrupt_dead_pid_returns_none(cfg, monkeypatch):
    monkeypatch.setattr(window, "find_claude_pid", lambda c: 999)

    def boom(pid, sig):
        raise ProcessLookupError()
    monkeypatch.setattr(window.os, "kill", boom)

    assert window.hard_interrupt(cfg) is None


# --- watchdog grace-window wiring ----------------------------------------------

def test_verify_hard_interrupt_skips_when_transcript_stops_growing(cfg, monkeypatch):
    """esc landed: mtime stays flat through the grace window -> no SIGINT."""
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    monkeypatch.setattr(watchdog.transcript, "mtime", lambda c: 1000.0)
    called = []
    monkeypatch.setattr(window, "hard_interrupt", lambda c: called.append(1) or 1)

    note = watchdog._verify_esc_or_hard_interrupt(cfg, grace_sec=6, trigger="fuse")
    assert note is None
    assert called == []


def test_verify_hard_interrupt_fires_when_transcript_keeps_growing(cfg, monkeypatch):
    """esc did not land: mtime keeps advancing -> SIGINT after grace, exactly once."""
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    ticks = iter([1000.0, 1001.0, 1002.0, 1003.0])
    monkeypatch.setattr(watchdog.transcript, "mtime", lambda c: next(ticks))
    calls = []
    monkeypatch.setattr(window, "hard_interrupt", lambda c: calls.append(1) or 777)

    note = watchdog._verify_esc_or_hard_interrupt(cfg, grace_sec=4, trigger="overrun")
    assert note == "hard-interrupt:overrun pid=777"
    assert len(calls) == 1  # max-once semantics


def test_verify_hard_interrupt_ambiguous_pid_logs_skip(cfg, monkeypatch):
    monkeypatch.setattr(watchdog.time, "sleep", lambda s: None)
    ticks = iter([1000.0, 1001.0, 1002.0])
    monkeypatch.setattr(watchdog.transcript, "mtime", lambda c: next(ticks))
    monkeypatch.setattr(window, "hard_interrupt", lambda c: None)

    note = watchdog._verify_esc_or_hard_interrupt(cfg, grace_sec=3, trigger="fuse")
    assert note == "hard-interrupt-skip:fuse (pid discovery ambiguous)"


def test_verify_hard_interrupt_disabled_by_config(cfg, monkeypatch):
    cfg["wake"]["watchdog"] = {**cfg["wake"].get("watchdog", {}), "hard_interrupt_enabled": False}
    called = []
    monkeypatch.setattr(window, "hard_interrupt", lambda c: called.append(1) or 1)

    note = watchdog._verify_esc_or_hard_interrupt(cfg, grace_sec=5, trigger="fuse")
    assert note is None
    assert called == []


def test_verify_hard_interrupt_no_transcript_skips(cfg, monkeypatch):
    monkeypatch.setattr(watchdog.transcript, "mtime", lambda c: None)
    called = []
    monkeypatch.setattr(window, "hard_interrupt", lambda c: called.append(1) or 1)

    note = watchdog._verify_esc_or_hard_interrupt(cfg, grace_sec=5, trigger="fuse")
    assert note is None
    assert called == []
