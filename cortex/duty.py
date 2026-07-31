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

from cortex import breaker, config

logger = logging.getLogger(__name__)

DUTY_FILE = "duty.json"

SHELL_CLI = "cli"
SHELL_TG = "tg"
# tg first everywhere a pair is walked: its kick is a socket write, while the
# cli pipeline can spend seconds in AppleScript before returning.
SHELLS = (SHELL_TG, SHELL_CLI)

MODE_CLI = "cli"
MODE_TG = "tg"
MODE_OFF = "off"
MODE_ALL = "all"
MODES = (MODE_CLI, MODE_TG, MODE_OFF, MODE_ALL)

HOLD_ALL = "all"
HOLDS = ("cli", "tg", HOLD_ALL)

FORCE_SLEPT = "duty"

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


def held_shells(hold: str | None) -> frozenset[str]:
    """Shells a hold value covers."""
    if not hold:
        return frozenset()
    if hold == HOLD_ALL:
        return frozenset(SHELLS)
    return frozenset({hold})


# --- transition ----------------------------------------------------------

def apply(cfg: dict, mode: str, *, now: datetime | None = None) -> dict:
    """Move duty to `mode` and act on the change: the new hold lands on disk
    FIRST, then a shell newly under it is put down, and only then is a released
    shell woken — so no instant exists where both shells are active. The
    incoming shell passes the fresh-vs-resume gate ([duty] thresholds) on its
    way up.

    Mode "all" kicks both shells whatever stood before (an explicit "run
    everything" must not depend on the previous hold); "off" holds both and
    kicks nothing. Callers own the [duty].enabled check and mode validation —
    an unknown mode writes through as a no-hold state rather than raising."""
    now = now or datetime.now(config.get_tz(cfg))
    config_dir = config.marrow_config_dir(cfg)
    before = held_shells(read(config_dir)["hold"])
    state = write(config_dir, mode, now=now)
    after = held_shells(state["hold"])

    put_down = SHELL_CLI in (after - before) and _put_down_cli(cfg)
    if str(mode).strip().lower() == MODE_ALL:
        waking = frozenset(SHELLS)
    else:
        waking = before - after
    fresh = []
    for shell in SHELLS:
        if shell not in waking:
            continue
        woke_fresh = (_wake_tg(cfg, now) if shell == SHELL_TG
                      else _wake_cli(cfg, now))
        if woke_fresh:
            fresh.append(shell)
    return {"mode": state["mode"], "hold": state["hold"],
            "put_down": put_down,
            "woken": [s for s in SHELLS if s in waking],
            "fresh": fresh}


def _put_down_cli(cfg: dict) -> bool:
    """End a live cli wake through the same proxy lie_down ct-pause uses (clears
    the awake marker, kills the watchdog, books NO next wake — a hold is a pure
    stop). Silent, and never fatal: the hold stands whether or not the window
    goes down cleanly."""
    from cortex import lie_down as lie_down_mod
    from cortex import wake_state

    if not wake_state.load(cfg).get("awake"):
        return False
    try:
        lie_down_mod.lie_down(cfg, force_slept=FORCE_SLEPT, book_alarm=False)
    except Exception as e:  # noqa: BLE001 — the hold outranks a failed put-down
        logger.warning("duty put-down of the cli window failed (%s)", e)
        return False
    return True


# --- fresh-vs-resume gate ------------------------------------------------

def _thresholds(cfg: dict) -> tuple[int, float]:
    d = cfg.get("duty") or {}
    return (int(d.get("fresh_token_threshold", 80000)),
            float(d.get("fresh_age_hours", 8)))


def _cli_needs_fresh(cfg: dict, now: datetime) -> bool:
    """Is the cli window too full or too stale to be resumed? Token source is
    fixed: the transcript's window occupancy, never a fresh parse. No transcript
    at all is not "old" — the wake pipeline already spawns fresh when there is
    nothing to resume."""
    from cortex import transcript

    tokens, hours = _thresholds(cfg)
    if transcript.window_tokens(cfg) > tokens:
        return True
    m = transcript.mtime(cfg)
    if m is None:
        return False
    return (now.timestamp() - m) > hours * 3600


def _tg_needs_fresh(cfg: dict, now: datetime) -> bool:
    """Same gate on the tg side, read from the ledger the bridge maintains:
    `occupancy` for size, the later of the last user / last note stamp for age.
    An empty ledger reads as neither — a shell that never ran has nothing to
    respawn."""
    from cortex import shell_ledger
    from cortex.occupancy import parse_due_at

    tokens, hours = _thresholds(cfg)
    d = shell_ledger.read(config.shell_state_dir(cfg), SHELL_TG)
    occ = d.get("occupancy")
    if isinstance(occ, int) and not isinstance(occ, bool) and occ > tokens:
        return True
    tz = config.get_tz(cfg)
    stamps = [s for s in (parse_due_at(d.get("last_user_ts"), tz),
                          parse_due_at(d.get("last_note_ts"), tz)) if s]
    if not stamps:
        return False
    return (now - max(stamps)).total_seconds() > hours * 3600


# --- wakes ---------------------------------------------------------------

def _wake_cli(cfg: dict, now: datetime) -> bool:
    """Wake cli through the standard ctl pipeline. Over the gate it goes up as a
    rotate: the flag makes the wake a fresh spawn, and the retired session id
    keeps every resume path off the window being left behind."""
    from cortex import ctl, wake_state

    fresh = _cli_needs_fresh(cfg, now)
    if fresh:
        wake_state.set_retired_sid(cfg, wake_state.load(cfg).get("transcript"))
        wake_state.set_rotated(cfg)
    ctl._wake_cli(cfg)
    return fresh


def _wake_tg(cfg: dict, now: datetime) -> bool:
    """Wake tg through the ctl plumbing: a due-now booking in its ledger (the
    durable half) plus a best-effort host kick. Over the gate the booking
    carries rotate_pending, so the bridge respawns instead of resuming."""
    from cortex import ctl

    fresh = _tg_needs_fresh(cfg, now)
    ctl._wake_tg(cfg, rotate=fresh)
    return fresh
