"""cortex.kick — external wake primitive (CLI main, callable as a subprocess).

A bridge (tg/wx) or a cli session pokes cortex awake to peek at a channel: a
watch reply/timeout fired, or her first morning message cleared the night flag.
A bare pacemaker_tick is NOT a wake primitive — a future next_wake_at is a
ledger hold and an awake window short-circuits to the watchdog. This module
does what the marrow user-wake reset does for cortex windows, but for the
sleeping case: under the wake_state flock + cancellation epoch it

  1. appends a rendered reason flag (config [kick].reason_*, cleared by note.py
     on delivery), then
  2. if cortex is ALREADY AWAKE -> stop. The reason lands via the next watchdog
     free-round note; no wake machinery is touched.
  3. if cortex is ASLEEP -> bump gen (cancel any in-flight alarm), clear the
     floor ledger hold (next_floor_due_at=None => DUE) + the durable next-wake
     ledger (next_wake_at), kill the recorded sentinel, then spawn ONE detached
     pacemaker_tick so the freed floor fires a real wake now.

Reason templates live in cortex config ([kick].reason_*), never hardcoded. The
reply reason carries her message text (--text) so cortex sees WHAT she said.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from cortex import config, wake_state


def _reason_text(cfg: dict, kind: str, **fields) -> str:
    """Render the config kick-reason template for `kind` with `fields`. Missing
    template or a bad placeholder -> the raw template (never raises)."""
    kcfg = cfg.get("kick", {}) or {}
    tmpl = str(kcfg.get(f"reason_{kind}") or "").strip()
    if not tmpl:
        return ""
    try:
        return tmpl.format(**{k: ("" if v is None else v) for k, v in fields.items()})
    except (KeyError, IndexError, ValueError):
        return tmpl


def _append_reason(cfg: dict, d: dict, reason: str) -> None:
    if not reason:
        return
    reasons = d.get("kick_reasons")
    if not isinstance(reasons, list):
        reasons = []
    reasons.append(reason)
    cap = int((cfg.get("kick", {}) or {}).get("max_reasons") or 8)
    d["kick_reasons"] = reasons[-cap:]


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


def _append_wake_signal(cfg: dict, line: str) -> bool:
    """Append a kick reason to wake_signal.log so the ear Monitor surfaces it as
    a session turn on an already-awake cortex (mirror of watchdog's tuck-in
    write). Best-effort: any I/O failure is swallowed. Returns True on write."""
    if not line:
        return False
    try:
        p = config.wake_signal_log_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(line + "\n")
        return True
    except (OSError, KeyError, TypeError):
        return False


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


def _live_wait(cfg: dict) -> bool:
    """True when a declared silence window (cortex.wait) is still in the future —
    an ear is armed and the wait-expiry free-round will surface the kick. Absent /
    past = no live wait: the awake window may have no ear, so the kick must open
    its own carrier round (F3)."""
    wu = wake_state.get_wait_until(cfg)
    return wu is not None and datetime.now(timezone.utc) < wu


def kick(cfg: dict, kind: str, **fields) -> dict:
    """Run one kick. `kind` (reply/timeout/morning) selects a config reason
    template; `fields` (id, text, minutes, ...) fill it. Returns a small result
    dict.

    Asleep -> reason flag + wake machinery (tick).
    Awake + interrupt (reply/timeout) + LIVE wait -> P12 C2 path: clear the wait
      in the SAME lock and push the reason down the ear (wake_signal.log).
    Awake + NO live wait (ANY kind) -> F3 carrier: queue the reason AND stamp an
      already-expired wait in the SAME lock, then spawn one tick so the tested
      wait-expiry free-round fires now and renders/consumes the reason inline —
      giving the kick a carrier round even when no ear is listening.
    Best-effort throughout: a lock/state failure drops the kick silently."""
    detail = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    reason = _reason_text(cfg, kind, **fields)
    morning = kind == "morning"
    interrupt = kind in ("reply", "timeout")
    live_wait = _live_wait(cfg)
    sentinel_pid = None
    was_awake = False
    flag_cleared = False
    wait_cleared = False
    open_round = False
    try:
        def _mutate(d):
            nonlocal sentinel_pid, was_awake, flag_cleared
            nonlocal wait_cleared, open_round
            was_awake = bool(d.get("awake"))
            # Awake + interrupt + live wait: reason rides the ear (out-of-lock),
            # NOT kick_reasons — else the next note render duplicates it. Every
            # other case queues the reason (wake note / free-round renders it).
            ear_ride = was_awake and interrupt and live_wait
            if not ear_ride:
                _append_reason(cfg, d, reason)
            # Morning kick clears the night flag under the SAME lock — awake or
            # asleep. Mid-night kicks (reply/timeout) never touch the flag.
            if morning and d.pop("mode", None) is not None:
                flag_cleared = True
            if was_awake:
                if ear_ride and d.pop("silence_wait_until", None) is not None:
                    wait_cleared = True
                elif not live_wait:
                    # F3: no live wait -> stamp an expired wait (atomic with the
                    # reason append) so the tick's wait-expiry free-round opens a
                    # carrier round for the queued reason.
                    d["silence_wait_until"] = (
                        datetime.now(timezone.utc) - timedelta(seconds=1)
                    ).isoformat()
                    open_round = True
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
        signalled = False
        if interrupt and live_wait:  # C2: reason rides the armed ear
            signalled = _append_wake_signal(cfg, reason)
        if open_round:
            _spawn_tick(cfg)  # tick -> wait-expiry free-round = carrier round
        return {"ok": True, "kind": kind, "awake": True, "ticked": False,
                "flag_cleared": flag_cleared, "wait_cleared": wait_cleared,
                "signalled": signalled, "round_opened": open_round}

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
    parser.add_argument("--text", default=None,
                        help="her reply body, rendered into the reply reason")
    args = parser.parse_args(argv)
    cfg = config.load()
    fields = {}
    if args.note_id is not None:
        fields["id"] = args.note_id
    if args.minutes is not None:
        fields["minutes"] = args.minutes
    if args.text is not None:
        fields["text"] = args.text
    result = kick(cfg, args.kind, **fields)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
