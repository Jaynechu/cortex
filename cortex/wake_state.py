"""Persistent window/wake runtime state (JSON file, sibling of affect_flag /
self_schedule). Holds the resident iTerm session id, the awake marker
(awake_since + wake_log row id + transcript hint) and the rotate guard. Kept
out of the pure PacemakerState so the decision core stays I/O-free; all paths
resolve from config (OSS-overridable via [paths]).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cortex import config

_AWAKE_KEYS = ("awake", "awake_since", "wake_log_id", "transcript",
               "silence_wait_until", "wait_count", "user_replied_this_wake",
               "tuck_pending")

_LOCK_TIMEOUT_SEC = 5.0


def wake_state_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wake_state_file") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "wake_state.json"


def wakeup_note_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wakeup_note_file") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "wakeup_note.md"


def watchdog_pidfile_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("watchdog_pidfile") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "watchdog.pid"


def lock_path(cfg: dict) -> Path:
    """Sibling .lock file guarding load-modify-write. Shared byte-for-byte with
    the marrow hook side so cross-process updates never lose each other."""
    return wake_state_path(cfg).with_suffix(".lock")


@contextlib.contextmanager
def _flock(cfg: dict):
    """Blocking exclusive flock on the sibling .lock file (short timeout via a
    non-blocking retry loop). Best-effort: if the lock cannot be acquired the
    write still proceeds (an unlocked write is the pre-existing behaviour), so a
    lock-dir hiccup never wedges a wake."""
    lp = lock_path(cfg)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        yield
        return
    deadline = _mono() + _LOCK_TIMEOUT_SEC
    got = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if _mono() >= deadline:
                    break
                _sleep(0.02)
        yield
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _mono() -> float:
    import time
    return time.monotonic()


def _sleep(sec: float) -> None:
    import time
    time.sleep(sec)


def load(cfg: dict) -> dict:
    p = wake_state_path(cfg)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except (OSError, ValueError):
        pass
    return {}


# Legacy keys from older schema versions, dropped on the next _save so state
# files converge (nothing reads these anymore — verified in both repos).
_DEAD_KEYS = ("rotated_at",)


def _save(cfg: dict, data: dict) -> None:
    """Atomic whole-file write: temp file in the same dir + os.replace so a
    reader never sees a half-written file. Callers hold _flock for the
    read-modify-write; _save alone is atomic but not serialised."""
    p = wake_state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    for k in _DEAD_KEYS:
        data.pop(k, None)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def update(cfg: dict, **kv) -> dict:
    with _flock(cfg):
        d = load(cfg)
        d.update(kv)
        _save(cfg, d)
        return d


def get_session_id(cfg: dict) -> str | None:
    return load(cfg).get("session_id")


def set_session_id(cfg: dict, sid: str) -> None:
    update(cfg, session_id=sid)


def is_awake(cfg: dict) -> bool:
    return bool(load(cfg).get("awake"))


def set_awake(cfg: dict, wake_log_id: int | None, transcript: str | None) -> None:
    update(cfg, awake=True,
           awake_since=datetime.now(timezone.utc).isoformat(),
           wake_log_id=wake_log_id, transcript=transcript, wait_count=0,
           user_replied_this_wake=False, tuck_pending=None)


def clear_awake(cfg: dict) -> None:
    with _flock(cfg):
        d = load(cfg)
        for k in _AWAKE_KEYS:
            d.pop(k, None)
        _save(cfg, d)


def user_replied_this_wake(cfg: dict) -> bool:
    """True once a real user message landed in the current wake (set by the
    marrow UserPromptSubmit hook). Drives the chat vs no-user silence tier."""
    return bool(load(cfg).get("user_replied_this_wake"))


def set_wait_until(cfg: dict, until_iso: str) -> None:
    """Declare a one-shot silence window: the watchdog holds off its routine
    timeout lie-down until this UTC instant (the model is e.g. waiting for the
    user to come back). Cleared once the watchdog acts on it (take_wait_until)."""
    update(cfg, silence_wait_until=until_iso)


def get_wait_until(cfg: dict) -> datetime | None:
    """Peek the declared silence deadline (UTC-aware) or None — the watchdog
    reads this every poll: still-future = keep holding; past/absent = the
    routine silent_max_min threshold applies. Non-destructive; the watchdog
    calls clear_wait_until() once it acts, so the extension fires only once."""
    raw = load(cfg).get("silence_wait_until")
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clear_wait_until(cfg: dict) -> None:
    """Reset the silence window to default (no permanent extension)."""
    with _flock(cfg):
        d = load(cfg)
        if d.pop("silence_wait_until", None) is not None:
            _save(cfg, d)


def get_wait_count(cfg: dict) -> int:
    """How many wait() calls have fired this wake (reset on wake start /
    lie_down). Absent -> 0."""
    try:
        return int(load(cfg).get("wait_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def bump_wait_count(cfg: dict) -> int:
    """Increment and persist the per-wake wait() counter; returns the new count."""
    with _flock(cfg):
        try:
            cur = int(load(cfg).get("wait_count", 0) or 0)
        except (TypeError, ValueError):
            cur = 0
        count = cur + 1
        d = load(cfg)
        d["wait_count"] = count
        _save(cfg, d)
        return count


def set_rotated(cfg: dict) -> None:
    """Rotate flag: lie_down sets it when the window grew past the rotate line so
    the NEXT pacemaker wake respawns a fresh window (SIGTERM claude + fresh spawn)
    instead of resuming the oversized one."""
    update(cfg, rotated=True)


def take_rotated(cfg: dict) -> bool:
    """Consume the rotate flag (read-and-clear). True = last lie_down asked the
    next wake to respawn the window fresh."""
    with _flock(cfg):
        d = load(cfg)
        val = bool(d.pop("rotated", False))
        if val:
            _save(cfg, d)
        return val


def get_sentinel_pid(cfg: dict) -> int | None:
    """Recorded pid of the one-shot exact-time wake sentinel (cortex.sentinel),
    or None. Every new lie_down kills this predecessor before arming a fresh one."""
    try:
        v = load(cfg).get("sentinel_pid")
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def set_sentinel_pid(cfg: dict, pid: int) -> None:
    update(cfg, sentinel_pid=pid)


def clear_sentinel_pid(cfg: dict, only_if_pid: int | None = None) -> None:
    """Drop the recorded sentinel pid. only_if_pid = self-guard: the sentinel
    clears its own record only when it still matches (a newer lie_down may have
    already re-armed a different pid). None = unconditional clear."""
    with _flock(cfg):
        d = load(cfg)
        cur = d.get("sentinel_pid")
        if only_if_pid is not None and cur is not None and int(cur) != int(only_if_pid):
            return
        if d.pop("sentinel_pid", None) is not None:
            _save(cfg, d)
