"""cortex.ctl — manual control CLI. Thin wrappers over the same wake/lie_down/
ledger paths the pacemaker uses, so a human can drive the resident window by
hand without racing the tick.

  wake            remote control: alive+awake -> on-duty text; alive+dormant ->
                  ear-signal wake now; dead -> report, never spawn
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


def _now(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def cmd_wake(cfg: dict) -> str:
    """/ct-wake remote control: the caller window is an ordinary remote and NEVER
    takes office. Three-way on the recorded resident (wake_state.resident_alive,
    the ONE liveness signal — recorded claude pid alive via kill -0):

      alive + awake   -> on-duty text, ZERO side effects.
      alive + dormant -> send a wake signal NOW (reuse the pacemaker fire path:
          a forced ctl decision through run_wake -> the resident's ear signal,
          exactly "the alarm firing early"). No spawn, no take-office here.
      dead            -> report death + a diagnostics hint; DO NOT spawn (spawn
          authority stays exclusively with the pacemaker schedule/reconcile).

    Manual take-office is abolished: env vars are birth-time-only, so a running
    claude can never be retro-fitted into a full cortex. The ONLY registration
    credential is the pacemaker spawn handshake (start_registration_handshake +
    marrow's claim on the fresh window's first prompt); this function never
    writes cortex_claude_sid.
    """
    if not wake_state.resident_alive(cfg):
        return _dead_text(cfg)
    if wake_state.is_awake(cfg):
        return str(cfg["wake"].get("ctl_wake_awake_text") or "").strip()
    # A human explicitly waking wants activity back — leave DND (ct-pause
    # documents /ct-wake as its exit).
    wake_state.set_paused(cfg, False)
    _signal_dormant_wake(cfg)
    return str(cfg["wake"].get("ctl_wake_signal_text") or "").strip()


def _signal_dormant_wake(cfg: dict) -> None:
    """Fire a wake NOW at a live-but-dormant resident by reusing the pacemaker
    fire path: a forced ctl decision through wake.run_wake, which routes to the
    resident's signal-file ear (respawn=False) — the same injection the sentinel
    tick would perform, no new mechanism. Best-effort: a window failure falls
    back to headless inside run_wake, and any error never wedges the caller."""
    from cortex import wake

    conn = db.connect(cfg)
    try:
        now = _now(cfg)
        decision = {"wake": True, "reasons": [], "gated_by": [],
                    "wake_reasons": "ctl",
                    "explanation": f"{now.strftime('%H:%M')} ctl remote wake"}
        try:
            wake.run_wake(conn, cfg, decision, now=now)
        except Exception:  # noqa: BLE001 — a signal failure must not wedge ctl
            pass
    finally:
        conn.close()


def _dead_text(cfg: dict) -> str:
    tmpl = str(cfg["wake"].get("ctl_wake_dead_text") or "").strip()
    backup_hint = str(config.wake_audit_log_path(cfg))
    return tmpl.replace("{backup_hint}", backup_hint)


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
        # Covert delivery: only the "⚙️ [CTL] mins=N rotate=B" marker line reaches
        # the window (bell via the ear Monitor; typed only if the ear is dead).
        # The full sleep instruction body is injected invisibly by the marrow hook
        # ([cortex].ctl_sleep_text), rendered from the mins/rotate args this line
        # carries — she never SEES the instruction, only the short marker.
        marker = str(cfg["wake"].get("ctl_sleep_marker") or "⚙️ [CTL]").strip()
        # human=true: an explicit ctl minutes choice, so the rendered lie_down
        # passes it unclamped (marrow ctl_sleep_text -> lie_down human_override).
        marker_line = (f"{marker} mins={int(mins)} "
                       f"rotate={'true' if rotate else 'false'} human=true")
        rung = window.deliver_covert_marker(cfg, marker_line)
        if rung != "none":
            return (f"sleep: instruction delivered ({rung}) "
                    f"(next_wake_min={int(mins)}, rotate={rotate})")
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
    sub.add_parser("wake", help="remote wake: signal the dormant resident, or report on-duty/dead")
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
