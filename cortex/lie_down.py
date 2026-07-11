"""cortex.lie_down — the command cortex runs to end a wake (the watchdog runs
it as proxy). It: clears due self-schedule entries, redraws the floor, records
this wake's token spend into ct_wake_log, kills the watchdog, flags a rotate
(next wake respawns a fresh window) when --rotate is passed, then clears the
awake marker. Rotate is an explicit session decision, no auto token judgement.

The interactive window returns control the moment a note is injected, so the
wake is NOT over when pacemaker_tick exits — this command (or a proxy) is what
actually ends a wake. force_slept marks a proxy lie-down (timeout/fuse/stale).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cortex import config, db, transcript, wake_state
from cortex.pacemaker import integration


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _clear_due_self_schedule(cfg: dict) -> int:
    """Drop self_schedule entries whose due_at <= now (in-scene => processed).
    Returns count removed. Future entries are kept."""
    p = config.self_schedule_path(cfg)
    try:
        items = json.loads(p.read_text()) if p.exists() else []
    except (OSError, ValueError):
        return 0
    if isinstance(items, dict):  # tolerate a bare dict (single entry, not wrapped in a list)
        items = [items]
    if not isinstance(items, list):
        return 0
    now = _now_utc()
    tz = ZoneInfo(cfg["core"]["timezone"])
    kept = []
    for it in items:
        due = it.get("due_at") if isinstance(it, dict) else None
        d = integration.parse_due_at(due, tz)  # tz-aware or naive-local (DST-correct)
        if d is not None and d <= now:
            continue
        kept.append(it)
    p.write_text(json.dumps(kept, ensure_ascii=False, indent=2))
    return len(items) - len(kept)


def _kill_watchdog(cfg: dict) -> None:
    p = wake_state.watchdog_pidfile_path(cfg)
    try:
        pid = int(p.read_text().strip())
    except (OSError, ValueError):
        return
    if pid != os.getpid():  # a proxy lie-down from the watchdog itself skips this
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    p.unlink(missing_ok=True)


def _record_tokens(conn, cfg: dict, state: dict, force_slept: str | None) -> int:
    """Record this wake's context occupancy (`tokens` — last assistant usage
    totals) into its ct_wake_log row. Occupancy grows monotonically within a
    window; the daily Cortex-Today metric sums each window's final occupancy
    (integration._finished_window_finals) plus the live window, so no per-wake
    net delta is stored. Returns the recorded occupancy."""
    tokens = transcript.window_tokens(cfg)
    wid = state.get("wake_log_id")
    if wid:
        try:
            conn.execute(
                "UPDATE ct_wake_log SET tokens=?, force_slept=? WHERE id=?",
                (tokens or None, force_slept, wid))
            conn.commit()
        except Exception:  # column race with concurrent migrate; best-effort
            pass
    return tokens


def lie_down(cfg: dict, force_slept: str | None = None, rotate: bool = False,
             next_wake_min: float | None = None) -> dict:
    """End the current wake. `next_wake_min` picks the next internal wake:
    an explicit minutes-from-now (clamped to [1, wake.next_wake_max]), or None =
    a uniform "dice" draw within the floor window (proxy paths: watchdog auto,
    stale reap, fuse — session-facing dice retired, N required at the MCP/CLI
    layer)."""
    from cortex.pacemaker.triggers import clamp_next_wake_minutes

    if next_wake_min is not None:
        next_wake_min = clamp_next_wake_minutes(next_wake_min, cfg)
    # Atomic awake claim: the watchdog (60s poll) and the tick awake-branch can
    # both run silence_action in the same window; only the caller that clears the
    # awake marker here proceeds, so the ct_wake_log update + floor redraw fire
    # once. A later caller (already cleared) no-ops. awake=true callers win as
    # before.
    state = wake_state.claim_lie_down(cfg)
    if state is None:
        return {"skipped": "not awake", "force_slept": force_slept,
                "rotated": rotate, "next_wake": None}
    conn = db.connect(cfg)
    try:
        tokens = _record_tokens(conn, cfg, state, force_slept)
        cleared = _clear_due_self_schedule(cfg)
        # wake redraw from now; next_floor drives the next_wake HH:MM the marrow
        # MCP wrapper surfaces to the session.
        next_floor = integration.lie_down(conn, cfg, minutes=next_wake_min)
        # Publish AFTER the floor redraw's save_state (which drops the key), so the
        # next wake's Plan Used line sees this wake's window occupancy (statusline
        # total: input + cache_read + cache_creation + output — the same metric
        # `tokens` already computed above for rotate/fuse), not the NET spend.
        integration.store_window_tokens(conn, tokens)
        _kill_watchdog(cfg)
        # Rotate is now an explicit session decision (the --rotate flag), not an
        # auto token judgement — set it and the NEXT pacemaker wake respawns a
        # fresh window (SIGTERM claude + fresh spawn) that reads the handoff.
        if rotate:
            wake_state.set_rotated(cfg)
        # awake marker already cleared atomically by claim_lie_down at entry.
        _arm_sentinel(cfg, next_floor)
        next_wake = _local_hm(next_floor, cfg)
        return {"tokens": tokens, "cleared_due": cleared,
                "force_slept": force_slept, "rotated": rotate,
                "next_wake": next_wake}
    finally:
        conn.close()


def _arm_sentinel(cfg: dict, next_floor: datetime) -> None:
    """Arm the one-shot exact-time wake sentinel for `next_floor`. Kills the
    recorded predecessor first (never orphaned — single detached process), then
    spawns a fresh one and records its pid. Gated by [wake].sentinel; false =
    tick-only (the launchd 5-min tick is the sole waker)."""
    _kill_sentinel(cfg)
    if not cfg["wake"].get("sentinel", True):
        return
    if next_floor is None:
        return
    seconds = (next_floor - _now_utc()).total_seconds()
    if seconds < 0:
        seconds = 0.0
    try:
        from cortex import sentinel
        pid = sentinel.spawn(cfg, seconds)
        wake_state.set_sentinel_pid(cfg, pid)
    except Exception:  # spawning the sentinel must never wedge the lie_down
        pass


def _kill_sentinel(cfg: dict) -> None:
    """SIGTERM the recorded sentinel predecessor and clear its record. A newer
    lie_down / user-wake reset calls this before arming a fresh one."""
    pid = wake_state.get_sentinel_pid(cfg)
    if pid is not None and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    wake_state.clear_sentinel_pid(cfg)


def _local_hm(dt: datetime | None, cfg: dict) -> str | None:
    """Next-floor datetime -> local HH:MM (config tz). None -> None."""
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(cfg["core"]["timezone"])).strftime("%H:%M")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End the current cortex wake")
    parser.add_argument("--force-slept", default=None,
                        help="mark a proxy lie-down (timeout|fuse|stale)")
    parser.add_argument("--rotate", action="store_true",
                        help="respawn a fresh window on the next wake")
    parser.add_argument("--next-wake-min", type=float, required=True,
                        help="minutes until the next internal wake "
                             "(required, clamped to [1, wake.next_wake_max])")
    args = parser.parse_args(argv)
    cfg = config.load()
    result = lie_down(cfg, force_slept=args.force_slept, rotate=args.rotate,
                      next_wake_min=args.next_wake_min)
    print(json.dumps(result, ensure_ascii=False))  # surface next_wake harmlessly
    return 0


if __name__ == "__main__":
    sys.exit(main())
