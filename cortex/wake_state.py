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

_AWAKE_KEYS = ("awake", "awake_since", "wake_log_id", "transcript")


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
           wake_log_id=wake_log_id, transcript=transcript)


def clear_awake(cfg: dict) -> None:
    d = load(cfg)
    for k in _AWAKE_KEYS:
        d.pop(k, None)
    _save(cfg, d)


def set_rotated(cfg: dict) -> None:
    """Belt-and-braces rotate flag: lie_down sets it when it types /clear so the
    next wake treats the window as fresh even before a new transcript exists."""
    update(cfg, rotated=True)


def take_rotated(cfg: dict) -> bool:
    """Consume the rotate flag (read-and-clear). True = last lie_down rotated."""
    d = load(cfg)
    val = bool(d.pop("rotated", False))
    if val:
        _save(cfg, d)
    return val
