"""Persistent window/wake runtime state (JSON file, sibling of affect_flag /
self_schedule). Holds the resident iTerm session id, the awake marker
(awake_since + wake_log row id + transcript hint) and the rotate guard. Kept
out of the pure PacemakerState so the decision core stays I/O-free; all paths
resolve from config (OSS-overridable via [paths]).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cortex import config

_AWAKE_KEYS = ("awake", "awake_since", "wake_log_id", "transcript",
               "silence_wait_until", "wait_count")


def wake_state_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wake_state_file") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "wake_state.json"


def wakeup_note_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wakeup_note_file") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "wakeup_note.md"


def watchdog_pidfile_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("watchdog_pidfile") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "watchdog.pid"


def load(cfg: dict) -> dict:
    p = wake_state_path(cfg)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except (OSError, ValueError):
        pass
    return {}


def _save(cfg: dict, data: dict) -> None:
    p = wake_state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def update(cfg: dict, **kv) -> dict:
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
           wake_log_id=wake_log_id, transcript=transcript, wait_count=0)


def clear_awake(cfg: dict) -> None:
    d = load(cfg)
    for k in _AWAKE_KEYS:
        d.pop(k, None)
    _save(cfg, d)


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
    count = get_wait_count(cfg) + 1
    update(cfg, wait_count=count)
    return count


def set_rotated(cfg: dict) -> None:
    """Rotate flag: lie_down sets it when the window grew past the rotate line so
    the NEXT pacemaker wake respawns a fresh window (SIGTERM claude + fresh spawn)
    instead of resuming the oversized one."""
    update(cfg, rotated=True)


def take_rotated(cfg: dict) -> bool:
    """Consume the rotate flag (read-and-clear). True = last lie_down asked the
    next wake to respawn the window fresh."""
    d = load(cfg)
    val = bool(d.pop("rotated", False))
    if val:
        _save(cfg, d)
    return val
