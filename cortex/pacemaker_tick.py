"""Pacemaker tick entry point (launchd, floor+jitter cadence). Log-only in
dry-run. dry_run=false + wake=1 -> real cortex wake (C3 wake runner).

Interactive-window reality (B3v): a wake is NOT over when this tick exits (the
tick returns the moment the note is injected). The wake-state marker + lie_down
command replace the dead process-mutex assumption:
  - awake marker set  -> skip the tick (no double-fire); stale marker -> reap;
  - window wake        -> watchdog/self owns lie_down (no floor redraw here);
  - headless / dry-run -> floor redraws here as before.
"""
from __future__ import annotations

import sys
import time

from cortex import config, db, transcript, wake_state
from cortex.pacemaker import integration
from cortex.wake import run_wake


def _handle_awake(conn, cfg: dict, st: dict, snap_gen: int | None = None) -> str:
    """A wake is in progress -> the awake gate: NEVER emit a wake signal while
    awake (the alarm stops once up). Instead run the silence check as a watchdog
    backup, so a dead/rebooted watchdog is not a blind spot. The tick fires every
    ~5 min, so the grace is approximated to a whole-tick granularity (the marker
    is stamped one tick, the auto sleep fires the next tick once grace has
    elapsed). Falls back to the stale reap only when the silence check held (e.g.
    a live wait_until) yet the transcript is long idle.

    `snap_gen` = the gen captured in the tick's opening snapshot. Before any
    consequential reap, re-validate it against the live epoch: a lie_down / user
    reset since the snapshot means the awake this tick saw is stale (BUG B at the
    tick level) — hold rather than act on a superseded snapshot."""
    from cortex.watchdog import silence_action
    if not _snapshot_awake_current(cfg, snap_gen):
        return "awake gate: snapshot superseded (gen moved) -> hold"
    # Watchdog-liveness heal (permanent-residency invariant): an awake window
    # must always have a live watchdog (per-wake poll + fuse). If the recorded
    # watchdog pid is dead (crash / reboot), respawn one now — the tick is the
    # 5-min backup, but the watchdog owns exact-time fuse + 60s silence polling.
    # Idempotent via watchdog.spawn's own singleton guard (a live pid = no-op).
    from cortex.wake import _window_alive
    if _window_alive(cfg):
        from cortex import watchdog
        if not watchdog._pid_alive(watchdog._recorded_watchdog_pid(cfg)):
            watchdog.spawn(cfg)
    mt = transcript.mtime(cfg)
    # Silence source for the awake gate = minutes since the last REAL user
    # message (assistant / system / ear injections don't reset it). None = 0.0 =
    # hold, matching watchdog.run. `idle` (file mtime) still drives the stale-reap
    # below (window liveness, not user silence); 1e9 when the transcript is gone.
    idle = (time.time() - mt) / 60.0 if mt else 1e9
    action = silence_action(cfg, transcript.user_silent_min(cfg) or 0.0)
    if action and not wake_state.load(cfg).get("awake"):
        return f"awake gate: {action} (idle {idle:.0f}min)"
    stale_min = float(cfg["wake"].get("stale", {}).get("threshold_min", 15))
    if idle >= stale_min:
        # Alive-but-quiet is normal (user reading/typing): transcript mtime is
        # not a liveness signal. Only reap when the resident window is actually
        # gone. Live-but-silent windows are handled by the silence tier above.
        from cortex.wake import _window_alive
        if _window_alive(cfg):
            return f"stale hold: window alive (idle {idle:.0f}min)"
        # Re-validate the snapshot epoch right before the reap: a user reset /
        # lie_down since the snapshot must cancel this stale-reap (fail closed).
        if not _snapshot_awake_current(cfg, snap_gen):
            return "stale hold: snapshot superseded (gen moved)"
        from cortex import lie_down as lie_down_mod
        r = lie_down_mod.lie_down(cfg, force_slept="stale")
        sys.stderr.write(
            f"[cortex] STALE WAKE reaped: idle={idle:.1f}min tokens={r['tokens']}\n")
        return f"stale wake reaped (idle {idle:.0f}min) -> proxy lie_down"
    if action:
        return f"awake gate: {action} (idle {idle:.0f}min)"
    return f"wake in progress (idle {idle:.0f}min) -> tick skipped"


def _snapshot_awake_current(cfg: dict, snap_gen: int | None) -> bool:
    """True if the tick's opening snapshot is still authoritative: the live epoch
    gen has not moved since the snapshot. snap_gen=None (legacy state with no
    gen) -> True (no epoch to compare, behave as before). Fail closed: a
    lock/parse failure reads as NOT current, so a doubtful reap is held."""
    if snap_gen is None:
        return True
    try:
        gen, _sid = wake_state.current_epoch(cfg)
    except wake_state.StateValidationError:
        return False
    return gen == snap_gen


def _in_night_window(now, cfg: dict) -> bool:
    """True when `now` (tz-aware, config tz) sits in [night.start, morning_start).
    Both are HH:MM local; the window wraps midnight (start >= morning_start), so
    22:00->06:00 spans two dates. Malformed bounds -> False (never auto-flag)."""
    n = config.night_cfg(cfg)
    try:
        sh, sm = (int(x) for x in str(n.get("start", "22:00")).split(":"))
        mh, mm = (int(x) for x in str(n.get("morning_start", "06:00")).split(":"))
    except (ValueError, AttributeError):
        return False
    cur = now.hour * 60 + now.minute
    start = sh * 60 + sm
    morning = mh * 60 + mm
    if start <= morning:
        return start <= cur < morning
    return cur >= start or cur < morning  # wraps midnight


def _night_self_check(cfg: dict, now) -> tuple[str | None, bool]:
    """Asleep-branch night bell-ringer — two facts only: all-channel silence +
    the bell. NO forced teardown. Returns (log line or None, short_circuit):
    short_circuit=True when the bell rang — the bell spawns its OWN wake tick, so
    the caller must NOT also run its wake path this tick (else two windows open).
    The formal night package (handoff + rotate + night band + flag) is cortex's
    OWN lie_down(mode='night'); this only makes cortex wake to run it.

    Preconditions: inside [night.start, morning_start), all-channel user silence
    (`global_user_silent_min`: max over marrow-db all channels + resident
    transcript) >= [night].silence_hours, the night flag unset, no turn in flight.
    In-flight guard: user-silence does NOT reset during a long assistant turn, so
    raw transcript mtime freshness ([night].in_flight_min) is the mid-turn guard.

    The bell (marker unset): mark the once-per-window night_kick flag atomically
    (asleep + flag-unset + not-yet-kicked, one strict-lock hold), then send ONE
    wake kick carrying [night].package_due_text so cortex wakes and runs its own
    four-piece (handoff enforced by the marrow gate). At most one bell per window.

    If the window never acts on the bell, NOTHING forces it: a dead window is
    handled at its next due by the existing died_no_handoff / ghost-handoff path
    (no forged rotate markers, so catchup is preserved)."""
    if wake_state.is_night_mode(cfg):
        return None, False  # already set -> no-op
    if not _in_night_window(now, cfg):
        return None, False
    n = config.night_cfg(cfg)
    silent_min = transcript.global_user_silent_min(cfg)
    if silent_min is None or silent_min < float(n.get("silence_hours", 1.5)) * 60.0:
        return None, False  # not silent long enough (or unknown -> hold)
    mt = transcript.mtime(cfg)
    if mt is not None:
        idle_min = (time.time() - mt) / 60.0
        if idle_min < float(n.get("in_flight_min", 5)):
            return "night self-check: turn in flight (mtime fresh) -> hold", False
    if bool(wake_state.load(cfg).get("night_kick")):
        return None, False  # bell already fired this window -> nothing forces it
    # Ring the bell once so cortex runs its own night package.
    if not wake_state.try_mark_night_kick(cfg):
        return None, False  # awake / flag / already-kicked landed under lock
    silent_h = silent_min / 60.0
    text = str(n.get("package_due_text") or "")
    if text:
        try:
            text = text.format(silent_h=f"{silent_h:.1f}")
        except (KeyError, IndexError, ValueError):
            pass
    from cortex import kick as kick_mod
    kick_mod.kick(cfg, "night_due", text=text or None)
    wake_state.wake_audit(cfg, "night_kick", "self-check",
                          f"silent={silent_min:.0f}min")
    return f"night self-check: bell sent (silent {silent_min:.0f}min)", True


def _parse_local(iso: str | None, cfg: dict):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    tz = ZoneInfo(cfg["core"]["timezone"])
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt


def _fire_dead_window(conn, cfg: dict, why: str) -> str:
    """A dead resident window whose ledger is due (or an accidental close) needs
    firing NOW. Reuse the tested wake path: run_wake's _window_wake_plan reads the
    rotate flag itself — rotated -> fresh spawn (handoff), else -> resume the
    recorded session. dry_run short-circuits to a log-only floor redraw.

    Every branch here handled the due ledger entry -> it must be consumed
    (cleared or replaced with the freshly redrawn floor), else the stale
    next_wake_at stays due and reconcile re-fires it again next tick (headless
    wake every ~5 min).

    Runs the same night/daily-budget gates run_tick would (state + context ->
    gates.run_gates), so an alarm due mid-night or after budget exhaustion
    does not fire anyway. Gated -> HOLD, ledger left UN-consumed (reconcile
    retries every tick, firing naturally once the gate opens — a night-time
    accidental close then resumes at gate end, matching the night design)."""
    from cortex.pacemaker import gates as gates_mod
    now = integration._now(cfg)
    state = integration.load_state(conn)
    context = integration.build_context(conn, cfg, now, state)
    gate_results = gates_mod.run_gates(state, context, cfg, now)
    gated_by = [g for g in gate_results if not g.allowed]
    if gated_by:
        names = ", ".join(g.name for g in gated_by)
        return f"reconcile ({why}) -> gated ({names}), ledger held for retry"
    if bool(cfg["pacemaker"].get("dry_run", True)):
        next_floor = integration.lie_down(conn, cfg)
        wake_state.set_next_wake_at(cfg, next_floor.isoformat() if next_floor else None)
        return f"reconcile ({why}) -> dry_run, floor redrawn only"
    decision = {"wake": True, "reasons": [], "gated_by": [],
                "wake_reasons": "reconcile",
                "explanation": f"{now.strftime('%H:%M')} reconcile: {why}"}
    result = run_wake(conn, cfg, decision, now=now)
    if result.get("mode") != "window":
        next_floor = integration.lie_down(conn, cfg)
        wake_state.set_next_wake_at(cfg, next_floor.isoformat() if next_floor else None)
    return f"reconcile ({why}) -> wake fired (mode={result.get('mode')})"


def _adopt_manual_window(cfg: dict) -> str | None:
    """Auto-adopt a cortex window the user opened `claude` in herself (in
    cortex_home) but never registered — so the tick treats it as the live
    resident this same tick instead of firing/spawning a duplicate. Runs INSIDE
    the shared spawn lock (wake._spawn_serialized) so it never races an actual
    spawn. Config-gated ([wake].auto_adopt, default on).

    Re-check liveness under the lock first (a spawn may have landed a resident
    between the caller's check and the lock). Then scan iTerm for an adoptable
    window (window.find_adoptable_window: interactive `claude` in cortex_home,
    newest start wins; headless `claude -p` excluded by construction). Adopt via
    the SAME atomic CAS the spawn path uses (wake_state.set_awake with the live
    epoch token, bump=False, session_id + claude transcript sid committed
    together) so a concurrent lie_down/reset cannot be overwritten. Returns a log
    line on adoption, else None (no candidate / adoption CAS lost / disabled)."""
    from cortex import wake, wake_state, window
    if not bool(cfg["wake"].get("auto_adopt", True)):
        return None
    with wake._spawn_serialized(cfg):
        if wake._window_alive(cfg):
            return None  # a resident landed under the lock -> nothing to adopt
        sid = window.find_adoptable_window(cfg)
        if not sid:
            return None
        claude_sid = window.claude_session_id(cfg)
        transcript_path = None
        if claude_sid:
            transcript_path = str(transcript.transcript_dir(cfg) / f"{claude_sid}.jsonl")
        try:
            token = wake_state.current_epoch(cfg)
        except wake_state.StateValidationError:
            return None
        new_epoch = wake_state.set_awake(
            cfg, None, transcript_path, expected_token=token, bump=False,
            session_id=sid)
        if new_epoch is None:
            return None  # a newer epoch superseded between capture and commit
        wake_state.wake_audit(cfg, "adopt_manual_window", sid,
                              f"claude_sid={claude_sid}")
        return f"adopted manual window {sid} (claude_sid={claude_sid})"


def _reconcile(conn, cfg: dict, st: dict, now) -> str | None:
    """Ledger reconcile (runs every tick, after night close). Returns a log line
    when it acts / short-circuits the rest of the tick, else None (let the normal
    flow proceed). HARD RULE: an ALIVE recorded session is never touched here.

      - paused                                   -> hold everything (DND).
      - window ALIVE                             -> None (normal flow / awake gate).
      - window dead + next_wake_at in the past   -> fire now (rotated?fresh:resume).
      - window dead + awake + no next_wake_at    -> accidental close -> resume now.
      - window dead + next_wake_at in the future -> hold (this tick / the 5-min
        cadence catches it at due time; no sentinel re-arm — the ledger is the
        source of truth, a re-arm would only duplicate the same fire). This
        hold is authoritative: it short-circuits main() so no other wake path
        (e.g. an overdue floor) can fire early while a future ledger alarm
        exists (e.g. right after `ctl sleep --min 30`)."""
    from cortex.wake import _window_alive

    if wake_state.is_paused(cfg):
        return "paused (DND): reconcile + reaps + injections held"
    if _window_alive(cfg):
        return None  # alive -> never touch; normal flow handles it
    # Before ANY dead-window fire/spawn: adopt a window the user opened herself.
    # A hit records it as the resident under the spawn lock -> treat as alive
    # this tick (no fire, no spawn), so she never re-registers her window.
    adopted = _adopt_manual_window(cfg)
    if adopted is not None:
        return adopted
    due = _parse_local(wake_state.get_next_wake_at(cfg), cfg)
    if due is not None and now >= due:
        return _fire_dead_window(conn, cfg, "ledger due, window dead")
    if st.get("awake") and due is None and wake_state.get_session_id(cfg):
        # An awake session whose window was closed with no scheduled wake: resume
        # immediately (1h prompt-cache tier — resume within ~5 min keeps it hot).
        return _fire_dead_window(conn, cfg, "accidental close of awake window")
    if due is not None:
        # Dead window, ledger not yet due -> hold; ledger is authoritative, no
        # other wake path (e.g. floor/run_tick) may fire early.
        return f"ledger hold: next wake {due.strftime('%H:%M')}, window dead"
    return None


def main() -> int:
    cfg = config.load()
    conn = db.connect(cfg)
    try:
        st = wake_state.load(cfg)
        # Snapshot gen: threaded into the awake branch so its consequential reaps
        # re-validate against the live epoch before firing (stale-snapshot guard).
        snap_gen = st.get("gen") if isinstance(st.get("gen"), int) else None
        if wake_state.is_paused(cfg):
            # DND holds everything (reconcile + reaps + injections).
            print(f"{db.utcnow_iso()} "
                  "paused (DND): reconcile + reaps + injections held", flush=True)
            return 0
        rc = _reconcile(conn, cfg, st, integration._now(cfg))
        if rc is not None:
            print(f"{db.utcnow_iso()} {rc}", flush=True)
            return 0
        if st.get("awake"):
            msg = _handle_awake(conn, cfg, st, snap_gen=snap_gen)
            print(f"{db.utcnow_iso()} {msg}", flush=True)
            return 0

        now = integration._now(cfg)
        # Asleep-branch night bell: ring once so cortex runs its own night
        # package; no forced teardown ever. The bell spawns its own wake tick,
        # so short-circuit here to avoid opening a second window.
        nc, nc_short = _night_self_check(cfg, now)
        if nc is not None:
            print(f"{db.utcnow_iso()} {nc}", flush=True)
        if nc_short:
            return 0
        t_tick = time.monotonic()
        decision = integration.run_tick(conn, cfg, now=now)
        t_gate = time.monotonic()
        dry_run = bool(cfg["pacemaker"].get("dry_run", True))

        if decision["wake"]:
            if dry_run:
                # log-only: still advance floor; ledger must mirror it (P1-2
                # rationale) so reconcile doesn't re-fire on a stale due time.
                next_floor = integration.lie_down(conn, cfg)
                wake_state.set_next_wake_at(
                    cfg, next_floor.isoformat() if next_floor else None)
            else:
                result = run_wake(conn, cfg, decision,
                                  tick_started=t_tick, gate_done=t_gate)
                if result.get("mode") != "window":
                    # headless path finished -> wake over, redraw floor now.
                    next_floor = integration.lie_down(conn, cfg)
                    wake_state.set_next_wake_at(
                        cfg, next_floor.isoformat() if next_floor else None)
                # window path: marker set, watchdog owns lie_down.
    finally:
        conn.close()
    print(f"{db.utcnow_iso()} {decision['explanation']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
