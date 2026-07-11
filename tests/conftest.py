from __future__ import annotations

import subprocess
import sqlite3

import pytest

from cortex import db

# Binaries that would touch the real machine (spawn iTerm, type into a window,
# steal focus, play a sound, probe processes). No test may reach these: a test
# that hits one un-mocked is an isolation bug, not a pass.
_BLOCKED_BINS = {
    "osascript", "afplay", "claude", "pgrep", "lsof", "ps", "security",
}


# Detached background modules a test must never really launch (they sleep +
# run a tick / poll loop, leaking a live process past the test).
_BLOCKED_MODULES = {"cortex.sentinel", "cortex.watchdog"}


def _guarded(orig, name):
    def wrapper(cmd, *a, **kw):
        first = None
        if isinstance(cmd, (list, tuple)) and cmd:
            first = str(cmd[0]).rsplit("/", 1)[-1]
        elif isinstance(cmd, str):
            first = cmd.strip().split()[0].rsplit("/", 1)[-1] if cmd.strip() else None
        if first in _BLOCKED_BINS:
            raise AssertionError(
                f"test isolation: real subprocess.{name}({first!r}) blocked — "
                f"mock this call (spawn/osascript/afplay/discovery) in the test")
        if isinstance(cmd, (list, tuple)) and "-m" in cmd:
            mods = {str(x) for x in cmd}
            if mods & _BLOCKED_MODULES:
                raise AssertionError(
                    f"test isolation: real subprocess.{name} spawning a cortex "
                    f"background module blocked — stub cortex.sentinel/watchdog "
                    f"Popen in the test")
        return orig(cmd, *a, **kw)
    return wrapper


@pytest.fixture(autouse=True)
def _block_real_processes(monkeypatch):
    """Fail loudly if any test reaches a machine-touching binary through an
    un-mocked subprocess call — never open a real window / play a sound / spawn
    claude during the suite. Tests that mock subprocess.run|Popen locally
    override this guard for their own scope."""
    monkeypatch.setattr(subprocess, "run", _guarded(subprocess.run, "run"))
    monkeypatch.setattr(subprocess, "Popen", _guarded(subprocess.Popen, "Popen"))


@pytest.fixture(autouse=True)
def _no_real_sentinel(monkeypatch):
    """lie_down arms a detached sentinel (subprocess.Popen(-m cortex.sentinel)).
    _arm_sentinel swallows spawn errors by design, so the process-guard's
    AssertionError alone would not stop a real spawn. Stub the spawn to a no-op
    fake pid so no test leaks a live sentinel; tests that assert sentinel
    behaviour override this with their own Popen stub."""
    try:
        import cortex.sentinel as _s
        monkeypatch.setattr(_s, "spawn", lambda cfg, seconds: 424242)
    except ImportError:
        pass


@pytest.fixture
def marrow_conn(tmp_path):
    conn = db.connect_path(tmp_path / "marrow.db")
    yield conn
    conn.close()


@pytest.fixture
def base_cfg(tmp_path):
    return {
        "core": {"timezone": "Australia/Melbourne"},
        "paths": {
            "marrow_db": str(tmp_path / "marrow.db"),
            "knowledgec_db": "",
            "geofence_file": "",
            "health_export": "",
        },
        "knowledgec": {"stream_name": "/app/usage", "categories": {"default": "uncategorized"}},
        "geofence": {"enabled": False},
        "health": {"enabled": False},
    }


def make_knowledgec_fixture(path, rows):
    """rows: list of (bundle_id, start_coredata, end_coredata) seconds since
    2001-01-01 UTC, matching real ZOBJECT column semantics."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ZOBJECT (ZSTREAMNAME TEXT, ZVALUESTRING TEXT, ZSTARTDATE REAL, ZENDDATE REAL)"
    )
    conn.executemany(
        "INSERT INTO ZOBJECT (ZSTREAMNAME, ZVALUESTRING, ZSTARTDATE, ZENDDATE) VALUES ('/app/usage', ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
