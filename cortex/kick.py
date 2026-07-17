"""cortex.kick — external wake primitive (CLI main, callable as a subprocess).

A bridge (tg/wx) or a cli session pokes cortex awake to peek at a channel: a
watch reply/timeout fired, or her first morning message cleared the night flag.
A bare pacemaker_tick is NOT a wake primitive — a future next_wake_at is a
ledger hold and an awake window short-circuits to the watchdog. This module
does what the marrow user-wake reset does for cortex windows, but for the
sleeping case: under the wake_state flock + cancellation epoch it

  1. appends a reason flag (rendered into the next wakeup note by note.py,
     then cleared), then
  2. if cortex is ALREADY AWAKE -> stop. The reason lands via the next
     watchdog free-round note; no wake machinery is touched.
  3. if cortex is ASLEEP -> bump gen (cancel any in-flight alarm), clear the
     floor ledger hold (next_floor_due_at=None => DUE) + the durable next-wake
     ledger (next_wake_at), kill the recorded sentinel, then spawn ONE detached
     pacemaker_tick so the freed floor fires a real wake now.

Reason templates live in cortex config ([kick].reason_*), never hardcoded.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys

from cortex import config, wake_state


_MAX_REASONS = 8  # cap the pending-flag list so a stuck bridge can't grow it


def _reason_text(cfg: dict, kind: str, **fields) -> str:
    kcfg = cfg.get("kick", {}) or {}
    tmpl = str(kcfg.get(f"reason_{kind}") or "").strip()
    if not tmpl:
        return ""
    try:
        return tmpl.format(**fields)
    except (KeyError, IndexError, ValueError):
        return tmpl


def _append_reason(d: dict, reason: str) -> None:
    if not reason:
        return
    reasons = d.get("kick_reasons")
    if not isinstance(reasons, list):
        reasons = []
    reasons.append(reason)
    d["kick_reasons"] = reasons[-_MAX_REASONS:]


def _clear_floor_deadline(cfg: dict) -> None:
    """Set next_floor_due_at=None on the ct_pacemaker_state JSON so the floor
    trigger reads DUE (mirror of marrow cortex_bridge._clear_floor_deadline).
    Best-effort: any db hiccup leaves the hold — the tick still self-heals."""
    import sqlite3

    from cortex import db

    try:
        conn = db.connect(cfg)
    except Exception:
        return
    try:
        row = conn.execute(
            "SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()
        if not row:
            return
        try:
            obj = json.loads(row["state"])
        except (ValueError, TypeError):
            return
        if obj.get("next_floor_due_at") is None:
            return
        obj["next_floor_due_at"] = None
        conn.execute(
            "UPDATE ct_pacemaker_state SET state = ? WHERE id = 1",
            (json.dumps(obj),))
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def _sigterm(pid) -> None:
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return
    if p <= 0 or p == os.getpid():
        return
    try:
        os.kill(p, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def _spawn_tick(cfg: dict) -> None:
    """Fire ONE detached pacemaker_tick. Never a bare in-process tick — the tick
    reads the freed floor + cleared ledger and runs the normal wake path."""
    log = wake_state.watchdog_pidfile_path(cfg).with_suffix(".kick.log")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "a")
        subprocess.Popen(
            [sys.executable, "-m", "cortex.pacemaker_tick"],
            stdout=f, stderr=f, stdin=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ},
        )
    except OSError:
        pass


def kick(cfg: dict, kind: str, **fields) -> dict:
    """Run one kick. `kind` selects the reason template (reply/timeout/morning);
    `fields` fills it (note_id, minutes, ...). Returns a small result dict.

    Awake cortex -> flag only (no tick). Asleep -> flag + wake machinery.
    Best-effort throughout: a lock/state failure drops the kick silently."""
    reason = _reason_text(cfg, kind, **fields)
    sentinel_pid = None
    was_awake = False
    try:
        def _mutate(d):
            nonlocal sentinel_pid, was_awake
            _append_reason(d, reason)
            was_awake = bool(d.get("awake"))
            if was_awake:
                return
            # Asleep: cancel any in-flight alarm epoch, drop the durable ledger,
            # release the recorded sentinel. Floor hold is cleared out-of-lock.
            d["gen"] = int(d.get("gen") or 0) + 1
            d.pop("next_wake_at", None)
            sentinel_pid = d.pop("sentinel_pid", None)

        wake_state.conditional_mutate(cfg, None, _mutate)
    except wake_state.StateValidationError:
        return {"ok": False, "reason": "state locked", "kind": kind}

    wake_state.wake_audit(cfg, "kick", kind, reason)
    if was_awake:
        return {"ok": True, "kind": kind, "awake": True, "ticked": False}

    _sigterm(sentinel_pid)
    _clear_floor_deadline(cfg)
    _spawn_tick(cfg)
    return {"ok": True, "kind": kind, "awake": False, "ticked": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poke cortex awake to peek at a channel (watch / morning).")
    parser.add_argument("--kind", required=True,
                        choices=("reply", "timeout", "morning"),
                        help="reason template to render into the wakeup note")
    parser.add_argument("--note-id", default=None,
                        help="outbox note id (reply / timeout)")
    parser.add_argument("--minutes", default=None,
                        help="silence minutes (timeout)")
    args = parser.parse_args(argv)
    cfg = config.load()
    fields = {}
    if args.note_id is not None:
        fields["id"] = args.note_id
    if args.minutes is not None:
        fields["minutes"] = args.minutes
    result = kick(cfg, args.kind, **fields)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
