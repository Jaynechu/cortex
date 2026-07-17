"""cortex.kick — external wake primitive (CLI main, callable as a subprocess).

A bridge (tg/wx) or a cli session pokes cortex awake to peek at a channel: a
watch reply/timeout fired, or her first morning message cleared the night flag.
A bare pacemaker_tick is NOT a wake primitive — a future next_wake_at is a
ledger hold and an awake window short-circuits to the watchdog. This module
does what the marrow user-wake reset does for cortex windows, but for the
sleeping case: under the wake_state flock + cancellation epoch it

  1. if cortex is ALREADY AWAKE -> stop (audit-only no-op). The next watchdog
     free-round note reflects context; no wake machinery is touched.
  2. if cortex is ASLEEP -> bump gen (cancel any in-flight alarm), clear the
     floor ledger hold (next_floor_due_at=None => DUE) + the durable next-wake
     ledger (next_wake_at), kill the recorded sentinel, then spawn ONE detached
     pacemaker_tick so the freed floor fires a real wake now.

No reason is injected into the note — context on wake is enough. The kind is
recorded ONLY in the wake-audit log (wake_audit("kick", kind, ...)).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys

from cortex import config, wake_state


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
    """Run one kick. `kind` (reply/timeout/morning) + `fields` (note_id, minutes,
    ...) are recorded in the wake-audit log only. Returns a small result dict.

    Awake cortex -> audit-only no-op (no tick, no wake machinery). Asleep ->
    wake machinery. Best-effort throughout: a lock/state failure drops the kick
    silently."""
    detail = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    morning = kind == "morning"
    sentinel_pid = None
    was_awake = False
    flag_cleared = False
    try:
        def _mutate(d):
            nonlocal sentinel_pid, was_awake, flag_cleared
            was_awake = bool(d.get("awake"))
            # Morning kick clears the night flag under the SAME lock — awake or
            # asleep. Day cadence resumes because build_context now reads no flag
            # (day floor bounds + no cap gate). Mid-night kicks (reply/timeout)
            # never touch the flag: dawdling is not morning.
            if morning and d.pop("mode", None) is not None:
                flag_cleared = True
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

    wake_state.wake_audit(cfg, "kick", kind,
                          f"{detail} flag_cleared={flag_cleared}".strip())
    if was_awake:
        return {"ok": True, "kind": kind, "awake": True, "ticked": False,
                "flag_cleared": flag_cleared}

    _sigterm(sentinel_pid)
    _clear_floor_deadline(cfg)
    _spawn_tick(cfg)
    return {"ok": True, "kind": kind, "awake": False, "ticked": True,
            "flag_cleared": flag_cleared}


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
