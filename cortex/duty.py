"""Duty rotation ("who is on duty") — at most one cortex shell running.

    <marrow config dir>/duty.json
        {"mode": "cli"|"tg"|"off"|"all", "hold": "cli"|"tg"|"all"|null, "ts": iso}
    file absent  = no duty hold
    unreadable   = treated as no hold (logged), never wedges a shell

`mode` is the intent; `hold` is the materialised pause scope — enforcement
reads `hold` only. The file sits beside breaker.json and IS the cross-repo
protocol: the tg bridge (synapse) ships its own independent reader, nothing is
imported across repos.

Duty never writes breaker.json. The effective pause of a shell is the union of
the manual breaker scope and the duty hold (see breaker.covers).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from cortex import breaker

logger = logging.getLogger(__name__)

DUTY_FILE = "duty.json"

MODE_CLI = "cli"
MODE_TG = "tg"
MODE_OFF = "off"
MODE_ALL = "all"
MODES = (MODE_CLI, MODE_TG, MODE_OFF, MODE_ALL)

HOLD_ALL = "all"
HOLDS = ("cli", "tg", HOLD_ALL)

_HOLD_BY_MODE = {
    MODE_OFF: HOLD_ALL,
    MODE_CLI: MODE_TG,
    MODE_TG: MODE_CLI,
    MODE_ALL: None,
}

CLEAR = {"mode": MODE_ALL, "hold": None, "ts": ""}


# --- paths ---------------------------------------------------------------

def duty_path(config_dir: Path | str) -> Path:
    """Sibling of breaker.json — the breaker path is the single derivation, so
    relocating one file moves both halves of the protocol together."""
    return breaker.breaker_path(config_dir).with_name(DUTY_FILE)


def hold_for(mode: str) -> str | None:
    """Pause scope a mode materialises: the shell on duty runs, the other is
    held."""
    return _HOLD_BY_MODE.get(str(mode).strip().lower())


# --- io ------------------------------------------------------------------

@contextlib.contextmanager
def _flock(p: Path):
    """Advisory lock on a `.lock` sibling. Best effort: a lock we cannot take
    must never wedge the caller."""
    lp = p.with_suffix(".lock")
    fd = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError as e:
        logger.warning("duty lock failed (%s) — proceeding unlocked", e)
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)


def _read_json(p: Path, default):
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, type(default)):
                return data
            logger.warning("duty file %s has unexpected shape — ignoring", p.name)
    except (OSError, ValueError) as e:
        logger.warning("duty file %s unreadable (%s) — treated as clear", p.name, e)
    return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# --- state ---------------------------------------------------------------

def read(config_dir: Path | str) -> dict:
    """Current duty state. An absent, corrupt or unknown-mode file reads as
    mode "all" with no hold — a broken duty file must never freeze a shell."""
    d = _read_json(duty_path(config_dir), {})
    mode = str(d.get("mode") or "").strip().lower()
    if mode not in MODES:
        return dict(CLEAR)
    hold = str(d.get("hold") or "").strip().lower()
    return {"mode": mode,
            "hold": hold if hold in HOLDS else None,
            "ts": str(d.get("ts") or "")}


def write(config_dir: Path | str, mode: str, *,
          now: datetime | None = None) -> dict:
    """Record `mode` with the hold it materialises. Last writer wins — a mode
    describes the whole two-shell world, so there is nothing to merge."""
    target = str(mode).strip().lower() or MODE_ALL
    p = duty_path(config_dir)
    state = {"mode": target,
             "hold": hold_for(target),
             "ts": (now or datetime.now().astimezone()).isoformat()}
    with _flock(p):
        _write_json(p, state)
    return state


def covers(config_dir: Path | str, shell: str) -> bool:
    """Does the duty hold cover `shell` right now?"""
    hold = read(config_dir)["hold"]
    if not hold:
        return False
    return hold in (HOLD_ALL, str(shell).strip().lower())
