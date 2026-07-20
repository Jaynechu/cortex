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
import subprocess
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
             next_wake_min: float | None = None, mode: str | None = None,
             human_override: bool = False) -> dict:
    """End the current wake. `next_wake_min` picks the next internal wake:
    an explicit minutes-from-now (clamped to the day/night/rotate band), or None
    = a uniform "dice" draw within the floor window (proxy paths: watchdog auto,
    stale reap, fuse — session-facing dice retired, N required at the MCP/CLI
    layer). `rotate` respawns a fresh window next wake and lowers the clamp floor
    to 0 (immediate successor allowed). `mode='night'` = the night package: rotate
    is forced (light window), the explicit next_wake_min clamps to
    [night.floor_min, night.floor_max], and the persistent night flag is set as a
    child of this claim's epoch. `human_override` (explicit ctl minutes) passes
    next_wake_min unclamped."""
    from cortex.pacemaker.triggers import clamp_next_wake_minutes

    night = mode == "night"
    if night:
        rotate = True  # night package always frees context with a fresh window
    # Rotate precondition (P17): refuse a rotate while THIS window's own
    # wake-signal ear tail is still alive — a live monitor task replays its
    # completion notification when the rotated window resumes. Refuse BEFORE
    # claim_lie_down so a refused call consumes no claim and leaves the wake
    # fully intact; the session TaskStops the monitor and calls again. Only the
    # single registered window may arm the ear (marrow fail-closed gate), so any
    # live tail on the resolved signal path is this window's own. Plain
    # (non-rotate) sleep never refuses — its ear must stay alive.
    if rotate and _own_ear_tail_alive(cfg):
        return {"skipped": "rotate_refused", "force_slept": force_slept,
                "rotated": False, "next_wake": None,
                "refused": _rotate_refuse_text(cfg)}
    if next_wake_min is not None:
        next_wake_min = clamp_next_wake_minutes(
            next_wake_min, cfg, rotate=rotate, night=night,
            human_override=human_override)
    # Atomic awake claim: the watchdog (60s poll) and the tick awake-branch can
    # both run silence_action in the same window; only the caller that clears the
    # awake marker here proceeds, so the ct_wake_log update + floor redraw fire
    # once. A later caller (already cleared) no-ops. awake=true callers win as
    # before. The claim BUMPS gen and hands back a claim_token (gen, state_id):
    # every late side effect below re-validates it under the strict lock, so a
    # user message / newer claim landing mid-body cancels this whole lie_down's
    # alarm chain (fail-closed cancellation epoch — BUG A).
    state = wake_state.claim_lie_down(cfg, force_slept=force_slept)
    if state is None:
        return {"skipped": "not awake", "force_slept": force_slept,
                "rotated": False, "next_wake": None}
    token = state.get("claim_token")
    conn = db.connect(cfg)
    try:
        tokens = _record_tokens(conn, cfg, state, force_slept)
        cleared = _clear_due_self_schedule(cfg)
        # A newer epoch (user reset / newer claim) already superseded this claim
        # -> the wake it was ending is now someone else's live wake. Abort every
        # remaining alarm side effect (floor redraw, watchdog kill, rotate,
        # ledger, sentinel) so we never re-arm against a stale generation.
        if not _token_ok(cfg, token):
            return {"tokens": tokens, "cleared_due": cleared,
                    "force_slept": force_slept, "rotated": False,
                    "next_wake": None, "superseded": True}
        # wake redraw from now; next_floor drives the next_wake HH:MM the marrow
        # MCP wrapper surfaces to the session.
        next_floor = integration.lie_down(conn, cfg, minutes=next_wake_min)
        # Publish AFTER the floor redraw's save_state (which drops the key), so the
        # next wake's Plan Used line sees this wake's window occupancy (statusline
        # total: input + cache_read + cache_creation + output — the same metric
        # `tokens` already computed above for rotate/fuse), not the NET spend.
        integration.store_window_tokens(conn, tokens)
        if _token_ok(cfg, token):
            _kill_watchdog(cfg)
        # Rotate is now an explicit session decision (the --rotate flag), not an
        # auto token judgement — set it and the NEXT pacemaker wake respawns a
        # fresh window (SIGTERM claude + fresh spawn) that reads the handoff. The
        # rotate/retire writes are conditional CHILDREN of the claim gen (they do
        # NOT bump — bumping would self-invalidate this claim's own sentinel), so
        # a superseding user reset suppresses them.
        rotated = False
        if rotate:
            try:
                rotated = bool(wake_state.conditional_mutate(
                    cfg, token, _mark_rotated(state.get("transcript"))))
            except wake_state.StateValidationError:
                pass  # superseded -> the newer epoch owns the window, no rotate
            if rotated:
                # Registration dropped (P16): physically kill the retiring
                # window's wake_signal ear tail so the ear disappears at rotate
                # time and cannot reappear until the successor legally claims
                # (marrow's fail-closed arm gate blocks any re-arm meanwhile).
                # Only the retiring window's own / stale zombie tails exist now;
                # the successor spawns later. Alarm sentinel/ledger/watchdog and
                # every other wake_state key are untouched.
                _kill_ear_tails(cfg)
        # Night flag: set AFTER rotate, BEFORE floor/sentinel — a conditional
        # CHILD of the claim gen (no bump, same as rotate) so a superseding user
        # reset suppresses it. The flag persists across wakes until the morning
        # kick clears it; day lie_downs never touch it.
        if night:
            try:
                wake_state.conditional_mutate(cfg, token, _set_night_mode)
            except wake_state.StateValidationError:
                pass  # superseded -> newer epoch owns the window, no flag
        # awake marker already cleared atomically by claim_lie_down at entry. The
        # sentinel arms at the real due time now (no gate-end clamp — that would
        # defeat the 120-360 roaming band).
        next_floor = _arm_sentinel(cfg, next_floor, token)
        next_wake = _local_hm(next_floor, cfg)
        # Rotate = hand over NOW: spawn the fresh successor immediately instead of
        # waiting for the ledger time. The retirement mutation already committed
        # (rotated True, cortex_claude_sid popped), so this only fires on a real
        # rotate. Spawn failure must NOT wedge the rotate — the retirement stays
        # landed; _spawn_successor alerts/audits best-effort. No double-spawn: the
        # spawn goes through _window_wake_plan which take_rotated()-consumes the
        # flag, so a racing pacemaker reconcile classifies "ear" and holds.
        if rotated:
            _spawn_successor(conn, cfg)
        if night:
            # C6 ack: INVISIBLE — audit-log line only, never a window inject.
            ack = (cfg.get("night", {}).get("ack_text") or "")
            if ack:
                try:
                    ack = ack.format(next_wake=next_wake or "?")
                except (KeyError, IndexError, ValueError):
                    pass
                wake_state.wake_audit(cfg, "night_package", "ack", ack)
        return {"tokens": tokens, "cleared_due": cleared,
                "force_slept": force_slept, "rotated": rotated,
                "next_wake": next_wake, "mode": mode}
    finally:
        conn.close()


def _spawn_successor(conn, cfg: dict) -> None:
    """Rotate succession: spawn the fresh successor window NOW via the existing
    pacemaker fire path — a forced decision through wake.run_wake. run_wake calls
    _window_wake_plan, which take_rotated()-consumes the rotate flag and classifies
    "fresh", so the fresh spawn does the rest (same shape as ctl's remote wake).
    Spawn authority stays inside cortex's own chain (this IS a pacemaker-family
    path).

    Idempotent (no double-spawn): the rotate flag is consumed atomically by
    _window_wake_plan's take_rotated() inside run_wake, so a racing reconcile
    that beat us finds no flag and classifies "ear"/"resume" instead of a second
    fresh spawn. If the flag is already gone here (someone consumed it), the
    successor is already being spawned -> skip.
    Best-effort: any failure to spawn is alerted via wake._alert_respawn_failed
    (or an audit line) and never wedges the rotate that already committed."""
    from cortex import wake, wake_state

    now = _now_local(cfg)
    try:
        if not wake_state.load(cfg).get("rotated"):
            return  # rotate flag already consumed -> successor already spawning
        decision = {"wake": True, "reasons": [], "gated_by": [],
                    "wake_reasons": "rotate",
                    "explanation": f"{now.strftime('%H:%M')} rotate succession"}
        wake.run_wake(conn, cfg, decision, now=now)
    except Exception as e:  # noqa: BLE001 - respawn must never wedge the rotate
        try:
            wake._alert_respawn_failed(conn, wake.wake_id_of(now),
                                       f"rotate succession: {str(e)[:150]}")
        except Exception:  # noqa: BLE001 - alert best-effort too
            wake_state.wake_audit(cfg, "rotate", "respawn_failed", str(e)[:150])


def _now_local(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def _token_ok(cfg: dict, token) -> bool:
    """True if the claim token still matches the live epoch (no bump / no
    delete-recreate since the claim). Fail-closed: a lock/parse failure reads as
    NOT ok, so a doubtful late side effect is dropped."""
    try:
        return wake_state.token_current(cfg, token)
    except wake_state.StateValidationError:
        return False


def _mark_rotated(transcript_path):
    """Mutator (used under conditional_mutate): set the one-shot rotate flag +
    the durable retired-sid, both children of the claim gen (no bump). retired_sid
    is the belt-and-braces guard the resume paths check so a stale transcript
    pointer never resumes the retired session."""
    from pathlib import Path

    def _m(d: dict):
        d["rotated"] = True
        d["retired_sid"] = Path(str(transcript_path)).stem if transcript_path else None
        return True
    return _m


def _set_night_mode(d: dict):
    """Mutator (used under conditional_mutate): set the persistent night flag as
    a child of the claim gen (no bump). Cleared later by the morning kick."""
    d["mode"] = "night"
    return True


def _arm_sentinel(cfg: dict, next_floor: datetime, token=None) -> datetime | None:
    """Persist the durable next-wake ledger and arm the one-shot exact-time wake
    sentinel for `next_floor`, all as CONDITIONAL children of the claim `token`.
    Ledger first (survives a compact/kill that loses the sentinel args). Kills the
    recorded predecessor sentinel (never orphaned), then spawns a fresh one
    carrying (gen, state_id, target) as CLI args and conditionally registers its
    pid. No night-end clamp: the flag drives low-frequency roaming, so a night
    alarm fires at its real due time.

    BUG A: if a user reset / newer claim bumps gen mid-body, the ledger write and
    the pid registration are both dropped under the strict lock; a sentinel that
    was already spawned before losing the race is SIGTERMed and NOT registered.
    Gated by [wake].sentinel; false = tick-only. Returns `next_floor` so the
    caller's reported next_wake always agrees with the ledger and sentinel."""
    _kill_sentinel(cfg)
    iso = _local_iso(next_floor, cfg) if next_floor is not None else None
    # Conditional ledger write: a bump since the claim -> this alarm is stale,
    # write nothing (fail closed). No bump: a plain (non-gen) ledger write.
    try:
        wake_state.conditional_mutate(cfg, token, _set_ledger(iso))
    except wake_state.StateValidationError:
        return next_floor  # superseded -> newer epoch owns the ledger + alarm
    if not cfg["wake"].get("sentinel", True):
        return next_floor
    if next_floor is None:
        return next_floor
    seconds = (next_floor - _now_utc()).total_seconds()
    if seconds < 0:
        seconds = 0.0
    try:
        from cortex import sentinel
        gen_sid = token if token is not None else (None, None)
        pid = sentinel.spawn(cfg, seconds, gen=gen_sid[0], state_id=gen_sid[1],
                             target_iso=iso)
    except Exception:  # spawning the sentinel must never wedge the lie_down
        return next_floor
    # Register the pid ONLY if the claim still owns the epoch. If a newer gen
    # slipped in between spawn and here, the just-spawned sentinel is an orphan
    # for a dead epoch: SIGTERM it and register nothing.
    try:
        wake_state.conditional_mutate(cfg, token, _set_sentinel_pid(pid))
    except wake_state.StateValidationError:
        _sigterm(pid)
    return next_floor


def _set_ledger(iso):
    def _m(d: dict):
        if iso is None:
            d.pop("next_wake_at", None)
        else:
            d["next_wake_at"] = iso
        return True
    return _m


def _set_sentinel_pid(pid):
    def _m(d: dict):
        d["sentinel_pid"] = pid
        return True
    return _m


def _sigterm(pid) -> None:
    try:
        if pid and int(pid) != os.getpid():
            os.kill(int(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, TypeError, ValueError):
        pass


def _own_ear_tail_alive(cfg: dict) -> bool:
    """True if a live wake_signal ear tail whose process ancestry chains back to
    THIS resident's claude process is still alive (the rotate precondition).
    Every tail on the box shares the same signal-log path (_ear_tail_pids has no
    per-window identity of its own), so ownership is established separately via
    ps ppid-walk to the registered resident's claude pid (window.find_claude_pid)
    — an orphan predecessor tail (parent died, reparented to launchd/init) or a
    foreign window's tail must NEVER block rotate; only the residue sweep
    (_kill_ear_tails) touches those."""
    pids = _ear_tail_pids(cfg)
    if not pids:
        return False
    from cortex import window
    resident_pid = window.find_claude_pid(cfg)
    if resident_pid is None:
        return False  # no verified resident pid -> never block on an unowned tail
    return any(_chains_to_ancestor(pid, resident_pid) for pid in pids)


_PPID_WALK_MAX_DEPTH = 20  # bounded: never loop forever on a corrupt ps chain


def _ppid_of(pid: int) -> int | None:
    try:
        proc = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    try:
        return int(out)
    except ValueError:
        return None


def _chains_to_ancestor(pid: int, ancestor_pid: int) -> bool:
    """True if `ancestor_pid` appears in `pid`'s parent chain (ps -o ppid= walk).
    Stops at pid 1/0 (launchd/init) or a broken/missing link -> not owned.
    Bounded depth so a corrupt chain can never spin forever."""
    current = pid
    for _ in range(_PPID_WALK_MAX_DEPTH):
        if current == ancestor_pid:
            return True
        parent = _ppid_of(current)
        if parent is None or parent <= 1:
            return False
        current = parent
    return False


def _rotate_refuse_text(cfg: dict) -> str:
    return str(cfg.get("wake", {}).get("rotate_refuse_text") or "").strip()


def _ear_tail_pids(cfg: dict) -> list[int]:
    """PIDs of live wake_signal ear tails (`tail … -f <signal_log>`). Match is
    narrowed to the exact resolved signal-log path (pgrep -f) so unrelated tails
    are never touched; our own pid is skipped. [] on any failure."""
    try:
        signal_log = str(config.wake_signal_log_path(cfg))
    except Exception:
        return []
    if not signal_log:
        return []
    try:
        proc = subprocess.run(
            ["pgrep", "-f", f"-f {signal_log}"],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if proc.returncode not in (0, 1):
        return []
    me = os.getpid()
    pids = []
    for raw in (proc.stdout or "").split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        if pid <= 0 or pid == me:
            continue
        pids.append(pid)
    return pids


def _kill_ear_tails(cfg: dict) -> int:
    """Best-effort residue sweep at rotate time (P17): SIGTERM any live
    wake_signal ear tail. The rotate precondition (own-tail-alive refusal) is the
    real guarantee; this only mops up orphan / stale zombie tails. Returns the
    count SIGTERMed, 0 on any failure."""
    killed = 0
    for pid in _ear_tail_pids(cfg):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        killed += 1
    return killed


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


def _local_iso(dt: datetime | None, cfg: dict) -> str | None:
    """Next-floor datetime -> local ISO (config tz) for the durable ledger."""
    if dt is None:
        return None
    return dt.astimezone(ZoneInfo(cfg["core"]["timezone"])).isoformat()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="End the current cortex wake")
    parser.add_argument("--force-slept", default=None,
                        help="mark a proxy lie-down (timeout|fuse|stale)")
    parser.add_argument("--rotate", action="store_true",
                        help="respawn a fresh window on the next wake")
    parser.add_argument("--next-wake-min", type=float, required=True,
                        help="minutes until the next internal wake (required, "
                             "clamped to the day or night band)")
    parser.add_argument("--mode", default=None, choices=("night",),
                        help="'night' = night package (forces rotate, night "
                             "floor band, sets the persistent night flag)")
    parser.add_argument("--human-override", action="store_true",
                        help="explicit ctl minutes pass unclamped (no day/night "
                             "band floor)")
    args = parser.parse_args(argv)
    cfg = config.load()
    result = lie_down(cfg, force_slept=args.force_slept, rotate=args.rotate,
                      next_wake_min=args.next_wake_min, mode=args.mode,
                      human_override=args.human_override)
    print(json.dumps(result, ensure_ascii=False))  # surface next_wake harmlessly
    return 0


if __name__ == "__main__":
    sys.exit(main())
