"""cortex.ctl — manual control CLI. Thin wrappers over the same wake/lie_down/
ledger paths the pacemaker uses, so a human can drive the resident window by
hand without racing the tick.

  wake            immediate wake via the standard run_wake pipeline (alive
                  resident -> ear signal; dead -> rotated?fresh:resume)
  sleep           awake resident -> inject a lie_down instruction; else
                  (dead, or alive-but-dormant) set the ledger directly
  pause           DND: hold tick reconcile / watchdog reaps / injections
  resume          leave DND; overdue ledger alarms fire on the next reconcile

Each subcommand prints one human-readable result line.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cortex import config, db, wake_state, window
from cortex.pacemaker import integration


def _now(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def cmd_wake(cfg: dict) -> str:
    from cortex.wake import run_wake, _window_alive
    # A human explicitly waking wants activity back — clear DND first.
    wake_state.set_paused(cfg, False)
    # Already-on-duty guard (singleton invariant): a resident window that is both
    # alive AND awake is already on duty — re-driving run_wake would re-set_awake
    # and spawn a second watchdog. The live session already has the human's
    # attention; refuse rather than double-activate. (Alive-but-dormant still
    # wakes: that is the intended ear path below.)
    if _window_alive(cfg) and wake_state.is_awake(cfg):
        return "wake: already awake on duty -> no-op (one resident)"
    # Always drive the standard wake pipeline (run_wake -> _window_wake_plan
    # + _window_wake), including the alive-resident ear path: it renders a
    # fresh note, sets the awake marker and starts the watchdog, and falls
    # back to headless on any AppleScript failure. Do not re-implement any
    # of that here — a hand-rolled signal-only path would skip set_awake and
    # the watchdog, letting the next tick double-wake and the eventual
    # lie_down hit claim_lie_down's "not awake" no-op.
    conn = db.connect(cfg)
    try:
        now = _now(cfg)
        decision = {"wake": True, "reasons": [], "gated_by": [],
                    "wake_reasons": "ctl",
                    "explanation": f"{now.strftime('%H:%M')} manual ctl wake"}
        result = run_wake(conn, cfg, decision, now=now)
        if result.get("mode") != "window":
            next_floor = integration.lie_down(conn, cfg)
            wake_state.set_next_wake_at(
                cfg, next_floor.isoformat() if next_floor else None)
        rotated = "fresh" if wake_state.load(cfg).get("rotated") else "resume/spawn"
        return f"wake: {rotated} (mode={result.get('mode')})"
    finally:
        conn.close()


def _resolve_minutes(cfg: dict, until: str | None, minutes: float | None) -> float:
    if until:
        hh, mm = until.split(":")
        now = _now(cfg)
        target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return max(1.0, (target - now).total_seconds() / 60.0)
    return float(minutes) if minutes is not None else 30.0


def cmd_sleep(cfg: dict, until: str | None, minutes: float | None, rotate: bool) -> str:
    mins = _resolve_minutes(cfg, until, minutes)
    # Gate on the awake marker, not window liveness: a resident window is
    # commonly alive-but-dormant (asleep, no wake in progress). Injecting a
    # lie_down prompt then hits claim_lie_down's "not awake" no-op and the
    # requested minutes/rotate are silently dropped.
    if wake_state.load(cfg).get("awake"):
        tmpl = cfg["wake"].get("ctl_sleep_prompt") or (
            "Wrap up this turn: {rotate}lie_down(next_wake_min={mins}{rotate_arg}).")
        rot = "write your handoff then " if rotate else ""
        rotate_arg = ", rotate=true" if rotate else ""
        prompt = (tmpl.replace("{mins}", str(int(mins)))
                  .replace("{rotate_arg}", rotate_arg)
                  .replace("{rotate}", rot))
        if window.inject_prompt(cfg, prompt):
            return f"sleep: instruction injected (next_wake_min={int(mins)}, rotate={rotate})"
        return "sleep: no resident window to inject into"
    # Not awake (dead window, or alive-but-dormant): set the ledger directly
    # so the next reconcile/tick fires it.
    due = _now(cfg) + timedelta(minutes=mins)
    wake_state.set_next_wake_at(cfg, due.isoformat())
    if rotate:
        wake_state.set_rotated(cfg)
    return f"sleep: ledger set for {due.strftime('%H:%M')} (rotate={rotate})"


def cmd_pause(cfg: dict) -> str:
    wake_state.set_paused(cfg, True)
    return "pause: DND on — tick reconcile, watchdog reaps and injections held"


def cmd_resume(cfg: dict) -> str:
    wake_state.set_paused(cfg, False)
    return "resume: DND off — overdue ledger alarms fire on the next reconcile"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cortex.ctl", description="Manual cortex control")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("wake", help="wake now (alive -> signal; dead -> fresh/resume)")
    sp = sub.add_parser("sleep", help="lie the live window down, or set the ledger")
    sp.add_argument("--until", default=None, help="wake at HH:MM (local)")
    sp.add_argument("--min", dest="minutes", type=float, default=None,
                    help="minutes until next wake")
    sp.add_argument("--rotate", action="store_true", help="respawn fresh next wake")
    sub.add_parser("pause", help="DND on")
    sub.add_parser("resume", help="DND off")
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.cmd == "wake":
        line = cmd_wake(cfg)
    elif args.cmd == "sleep":
        line = cmd_sleep(cfg, args.until, args.minutes, args.rotate)
    elif args.cmd == "pause":
        line = cmd_pause(cfg)
    else:
        line = cmd_resume(cfg)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
