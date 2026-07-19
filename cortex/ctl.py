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

from cortex import config, db, transcript, wake_state, window
from cortex.pacemaker import integration


def _now(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def cmd_wake(cfg: dict) -> str:
    """In-window take-office (P17): /ct-wake runs INSIDE the target window (a
    manually opened cortex window). It never spawns/resumes/hunts — the pacemaker
    owns spawning.

    Registration (stage-then-promote, P17 gap fix): marrow's caller-side
    PreToolUse hook has already STAGED the calling window's own claude sid as
    wake_state's pending_claim before this runs (marrow, not cortex, has
    reliable access to the caller's sid). This function is the sole
    promoter/discarder — a refused wake discards the staged claim WITHOUT ever
    touching cortex_claude_sid, so the true resident's registration and
    is_cortex_session identity stay fully intact. No pending_claim staged
    (e.g. ctl wake invoked outside a claude window, or the hook missed) ->
    promote is a no-op either way; take-office still proceeds on the existing
    liveness semantics below, registration simply stays whatever it already was.

    Resident-first (codex P0 fix): `rotated`/`retired_sid` are NOT the grant
    gate — `rotated` is one-shot (consumed by the next pacemaker wake) and
    `retired_sid` is a PERSISTENT belt-and-braces marker (never cleared, records
    only the last-ever retired sid) — neither tells you whether a resident is
    on duty RIGHT NOW. The only live signal is whether the recorded resident's
    claude process is actually alive this instant (window.find_claude_pid):

      live resident, THIS ctl process is NOT its own descendant (a foreign
          window woke while another is on duty) -> refuse, zero side effects
          (registration untouched, staged claim discarded).
      live resident, THIS ctl process runs INSIDE it (self re-wake of a
          dormant resident) -> take office, promote (idempotent — same sid).
      no live resident (dead or none) -> take office, promote. died_no_handoff
          fires only when the dead resident did NOT cleanly retire: neither the
          one-shot `rotated` flag is still pending (not yet consumed by a
          pacemaker wake) NOR does its own transcript sid match the durable
          `retired_sid` (that exact session was the one properly rotated).
    """
    import os
    from cortex.lie_down import _chains_to_ancestor, _kill_sentinel

    resident_pid = window.find_claude_pid(cfg)
    if resident_pid is not None and not _chains_to_ancestor(os.getpid(), resident_pid):
        wake_state.discard_pending_claim(cfg)
        return str(cfg["wake"].get("ctl_wake_resident_text") or "").strip()

    died_no_handoff = resident_pid is None and not _cleanly_retired(cfg)

    # Take office. set_awake BUMPS gen and clears next_wake_at atomically, so any
    # sentinel/tick armed for the old due finds its token stale and no-ops
    # (sentinel fire-time epoch check) — the alarm is cancelled atomically vs the
    # tick. _kill_sentinel is best-effort cleanup on top of that guard.
    conn = db.connect(cfg)
    try:
        now = _now(cfg)
        tpath = transcript.newest(cfg)
        wid = _record_wake_row(conn, now)
        wake_state.take_rotated(cfg)  # consume the one-shot flag if set
        wake_state.set_awake(cfg, wid, str(tpath) if tpath else None)
        wake_state.promote_pending_claim(cfg)
        _kill_sentinel(cfg)
        # A human explicitly waking wants activity back — leave DND (ct-pause
        # documents /ct-wake as its exit).
        wake_state.set_paused(cfg, False)
        if died_no_handoff:
            _ghost_handoff_hint(conn, cfg, now)
        return _arm_line(cfg)
    finally:
        conn.close()


def _cleanly_retired(cfg: dict) -> bool:
    """True iff the dead resident properly rotated before it died — either the
    one-shot `rotated` flag is still pending (set at rotate time, not yet
    consumed by a pacemaker wake), or the dead resident's OWN transcript sid
    matches the durable `retired_sid` marker (that exact session was the one a
    prior lie_down(rotate=True) retired). `retired_sid` alone (without the sid
    match) is NEVER sufficient — it is sticky forever once any rotate has ever
    happened on this machine, so a bare presence check would treat every crash
    after the first-ever rotate as a clean retirement (the P0 bug)."""
    st = wake_state.load(cfg)
    if bool(st.get("rotated")):
        return True
    from pathlib import Path
    transcript_path = st.get("transcript")
    sid = Path(str(transcript_path)).stem if transcript_path else None
    retired_sid = wake_state.get_retired_sid(cfg)
    return bool(sid) and sid == retired_sid


def _record_wake_row(conn, now: datetime) -> int | None:
    """Log this take-office as an activation wake row so the note's Last-wake
    segment counts it. Best-effort."""
    try:
        return integration.log_activation_wake_row(conn, now, "ctl-wake")
    except Exception:  # noqa: BLE001
        return None


def _ghost_handoff_hint(conn, cfg: dict, now: datetime) -> None:
    """Dead prev window left no handoff: write a fresh note carrying the existing
    died_no_handoff catchup line so this window ghost-writes the handoff. Reuses
    the note/window plumbing — no new mechanism. Best-effort."""
    try:
        from cortex.wake import assemble_note
        note_text = assemble_note(conn, cfg, now, died_no_handoff=True)
        window.write_note(cfg, note_text)
    except Exception:  # noqa: BLE001
        pass


def _arm_line(cfg: dict) -> str:
    tmpl = str(cfg["wake"].get("ctl_wake_arm_text") or "").strip()
    signal_log = str(config.wake_signal_log_path(cfg))
    return tmpl.replace("{signal_log}", signal_log)


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
