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


def _record_tokens(conn, cfg: dict, state: dict, force_slept: str | None) -> tuple[int, int]:
    """Record this wake's total context occupancy (`tokens`, drives rotate/fuse)
    and its NET spend DELTA (`net_tokens` — cache-miss rewrite + output; drives
    the daily budget gate + Budget note line).

    transcript.net_tokens is the WHOLE-SESSION cumulative (sum over every usage
    row), so a window lying down N times a night would re-count that running
    total N times. Record only the delta since the last reported cumulative:
    `delta = cum - prev` when monotonic; a fresh window's cum starts near 0
    (below prev) -> treat the full cum as the delta and reset the baseline.
    Persist the new cumulative as the next delta's baseline. Returns
    (tokens, delta)."""
    tokens = transcript.window_tokens(cfg)
    cum = transcript.net_tokens(cfg)
    prev = integration.load_net_reported(conn)
    delta = cum - prev if cum >= prev else cum
    wid = state.get("wake_log_id")
    if wid:
        try:
            conn.execute(
                "UPDATE ct_wake_log SET tokens=?, net_tokens=?, force_slept=? WHERE id=?",
                (tokens or None, delta or None, force_slept, wid))
            conn.commit()
        except Exception:  # column race with concurrent migrate; best-effort
            pass
    # Publish the new baseline. Merged into the raw state JSON (ON CONFLICT), so
    # the later floor-redraw save_state (which preserves side-channel keys) does
    # not drop it — same survival mechanism as window_tokens.
    integration.store_net_reported(conn, cum)
    return tokens, delta


def lie_down(cfg: dict, force_slept: str | None = None, rotate: bool = False,
             next_wake_min: float | None = None) -> dict:
    """End the current wake. `next_wake_min` picks the next internal wake:
    an explicit minutes-from-now (clamped to the wake window), or None = a
    uniform "dice" draw within the window (preserves prior behaviour)."""
    conn = db.connect(cfg)
    try:
        state = wake_state.load(cfg)
        tokens, net = _record_tokens(conn, cfg, state, force_slept)
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
        wake_state.clear_awake(cfg)
        next_wake = _local_hm(next_floor, cfg)
        return {"tokens": tokens, "cleared_due": cleared,
                "force_slept": force_slept, "rotated": rotate,
                "next_wake": next_wake}
    finally:
        conn.close()


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
    parser.add_argument("--next-wake-min", type=float, default=None,
                        help="minutes until the next internal wake (clamped to "
                             "the wake window); omit for a uniform dice draw")
    args = parser.parse_args(argv)
    cfg = config.load()
    result = lie_down(cfg, force_slept=args.force_slept, rotate=args.rotate,
                      next_wake_min=args.next_wake_min)
    print(json.dumps(result, ensure_ascii=False))  # surface next_wake harmlessly
    return 0


if __name__ == "__main__":
    sys.exit(main())
