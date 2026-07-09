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
    """A wake is in progress. Reap it if the transcript has been idle past the
    stale threshold (watchdog presumed dead -> never leave the marker wedged);
    otherwise skip this tick so we never double-fire."""
    stale_min = float(cfg["wake"].get("stale", {}).get("threshold_min", 15))
    mt = transcript.mtime(cfg)
    idle = (time.time() - mt) / 60.0 if mt else 1e9
    if idle >= stale_min:
        from cortex import lie_down as lie_down_mod
        r = lie_down_mod.lie_down(cfg, force_slept="stale")
        sys.stderr.write(
            f"[cortex] STALE WAKE reaped: idle={idle:.1f}min tokens={r['tokens']}\n")
        return f"stale wake reaped (idle {idle:.0f}min) -> proxy lie_down"
    return f"wake in progress (idle {idle:.0f}min) -> tick skipped"


def main() -> int:
    cfg = config.load()
    conn = db.connect(cfg)
    try:
        st = wake_state.load(cfg)
        nc = _night_close(cfg, integration._now(cfg), st)
        if nc:
            print(f"{db.utcnow_iso()} {nc}", flush=True)
        if st.get("awake"):
            msg = _handle_awake(conn, cfg, st)
            print(f"{db.utcnow_iso()} {msg}", flush=True)
            return 0

        t_tick = time.monotonic()
        decision = integration.run_tick(conn, cfg)
        t_gate = time.monotonic()
        dry_run = bool(cfg["pacemaker"].get("dry_run", True))

        if decision["wake"]:
            if dry_run:
                integration.lie_down(conn, cfg)  # log-only: still advance floor
            else:
                result = run_wake(conn, cfg, decision,
                                  tick_started=t_tick, gate_done=t_gate)
                mode = result.get("mode")
                if mode == "schedule":
                    pass  # fresh duty window is self-contained; floor untouched.
                elif mode != "window":
                    # headless path finished -> wake over, redraw floor now.
                    integration.lie_down(conn, cfg)
                # window path: marker set, watchdog owns lie_down.
    finally:
        conn.close()
    print(f"{db.utcnow_iso()} {decision['explanation']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
