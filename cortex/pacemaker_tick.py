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
from cortex.pacemaker import integration, gates
from cortex.wake import run_wake


def _night_close(cfg: dict, now, st: dict) -> str | None:
    """Night close (replaces daily rebirth). When the night window opens and the
    resident window is still up, send it a one-shot wrap-up instruction (the
    existing inject-after-turn path) telling it to write its handoff and lie_down.
    Once it is down (or if it was already down at the window open), the session is
    marked non-resumable via the rotate flag, so the first post-night wake is a
    plain fresh spawn that reads the handoff via SessionStart. Returns a log line
    when it acts, else None. No SIGINT; the watchdog fuse ladder is untouched."""
    from cortex import window

    key = gates.night_key(cfg, now)
    if key is None:
        return None
    ncfg = cfg.get("gates", {}).get("night", {}) or {}
    if st.get("awake"):
        # Still awake in the night window -> ask it once to wrap up (after the
        # current turn). Marking non-resumable waits until it actually lies down.
        if st.get("night_wrap_key") == key:
            return None
        prompt = ncfg.get("close_prompt") or ""
        wake_state.update(cfg, night_wrap_key=key)
        if prompt and window.inject_prompt(cfg, prompt):
            return "night close: wrap-up injected"
        return "night close: no resident window to wrap up"
    # Not awake in the night window: mark the (idle) resident session
    # non-resumable, once per night. Skip if no session exists (already fresh).
    if st.get("night_rotated_key") == key or not wake_state.get_session_id(cfg):
        return None
    wake_state.set_rotated(cfg)
    wake_state.update(cfg, night_rotated_key=key)
    return "night close: resident session marked non-resumable"


def _handle_awake(conn, cfg: dict, st: dict) -> str:
    """A wake is in progress -> the awake gate: NEVER emit a wake signal while
    awake (the alarm stops once up). Instead run the two-tier silence checks as
    a watchdog backup, so a dead/rebooted watchdog is not a blind spot. The tick
    fires every ~5 min, so the chat-tier grace is approximated to a whole-tick
    granularity (the marker is stamped one tick, the auto sleep fires the next
    tick once grace has elapsed). Falls back to the stale reap only when the
    silence tier held (e.g. a live wait_until) yet the transcript is long idle."""
    from cortex.watchdog import silence_action
    mt = transcript.mtime(cfg)
    # No transcript mtime = no data. silence_action must see 0.0 (hold, don't
    # insta-sleep on a missing transcript — matches watchdog.run's fallback so
    # the shared silence_action gets one "no data" behaviour). The stale-reap
    # below keeps 1e9 so a genuinely gone transcript is still reaped.
    idle = (time.time() - mt) / 60.0 if mt else 1e9
    action = silence_action(cfg, idle if mt else 0.0)
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
        from cortex import lie_down as lie_down_mod
        r = lie_down_mod.lie_down(cfg, force_slept="stale")
        sys.stderr.write(
            f"[cortex] STALE WAKE reaped: idle={idle:.1f}min tokens={r['tokens']}\n")
        return f"stale wake reaped (idle {idle:.0f}min) -> proxy lie_down"
    if action:
        return f"awake gate: {action} (idle {idle:.0f}min)"
    return f"wake in progress (idle {idle:.0f}min) -> tick skipped"


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
                "explanation": f"{now.strftime('%H:%M')} reconcile: {why}"}
    result = run_wake(conn, cfg, decision, now=now)
    if result.get("mode") != "window":
        next_floor = integration.lie_down(conn, cfg)
        wake_state.set_next_wake_at(cfg, next_floor.isoformat() if next_floor else None)
    return f"reconcile ({why}) -> wake fired (mode={result.get('mode')})"


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
        if wake_state.is_paused(cfg):
            # DND holds everything, including night-close's wrap-up injection —
            # must be checked before _night_close, not just inside _reconcile.
            print(f"{db.utcnow_iso()} "
                  "paused (DND): reconcile + reaps + injections held", flush=True)
            return 0
        nc = _night_close(cfg, integration._now(cfg), st)
        if nc:
            print(f"{db.utcnow_iso()} {nc}", flush=True)
        rc = _reconcile(conn, cfg, st, integration._now(cfg))
        if rc is not None:
            print(f"{db.utcnow_iso()} {rc}", flush=True)
            return 0
        if st.get("awake"):
            msg = _handle_awake(conn, cfg, st)
            print(f"{db.utcnow_iso()} {msg}", flush=True)
            return 0

        now = integration._now(cfg)
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
