"""Per-wake watchdog: spawned at note injection, killed at lie_down, never
resident. Every poll_sec it reads the transcript (mtime + window tokens) and
the awake marker, applying two judgements:
  (b) silent past silent_max_min without lie_down -> proxy lie_down (timeout).
      The routine end: user replies keep the transcript mtime fresh, so an
      active conversation never times out mid-turn.
  (c) window tokens >= fuse -> esc + proxy lie_down (fuse) — the runaway fuse.
On the fuse path, esc is followed by a grace window (hard_interrupt_grace_sec):
if the transcript is still growing (esc didn't land, e.g. no focus), SIGINT the
resident claude process — a guaranteed esc-equivalent, at most once per trigger.
Three-layer trace: esc/inject/SIGINT -> ct_wake_log.force_slept -> next note's
Last wake (watchdog.log carries the pid + skip/ambiguous detail).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from cortex import config, db, transcript, wake_state, window
from cortex.pacemaker import integration


def spawn(cfg: dict) -> int:
    """Launch a detached per-wake watchdog process (`python -m cortex.watchdog`)
    that outlives the pacemaker tick. Returns its pid."""
    log = wake_state.watchdog_pidfile_path(cfg).with_suffix(".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a")
    p = subprocess.Popen(
        [sys.executable, "-m", "cortex.watchdog"],
        stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ},
    )
    return p.pid


def _verify_esc_or_hard_interrupt(cfg: dict, grace_sec: float, trigger: str) -> str | None:
    """After an esc, poll the transcript mtime for up to grace_sec. If it's
    still growing (mid-generation, esc didn't land), SIGINT the resident
    claude process as a guaranteed fallback. Returns the pid string logged
    into the wake explanation, or None if esc alone was enough / disabled /
    discovery was ambiguous."""
    wcfg = cfg["wake"].get("watchdog", {})
    if not wcfg.get("hard_interrupt_enabled", True):
        return None
    before = transcript.mtime(cfg)
    if before is None:
        return None
    step = 2.0
    waited = 0.0
    while waited < grace_sec:
        time.sleep(min(step, grace_sec - waited))
        waited += step
        after = transcript.mtime(cfg)
        if after is None or after <= before:
            return None  # stopped growing -> esc landed, no hard interrupt needed
    pid = window.hard_interrupt(cfg)
    if pid is None:
        return f"hard-interrupt-skip:{trigger} (pid discovery ambiguous)"
    return f"hard-interrupt:{trigger} pid={pid}"


def run(cfg: dict) -> int:
    from cortex import lie_down as lie_down_mod

    wcfg = cfg["wake"].get("watchdog", {})
    poll = int(wcfg.get("poll_sec", 60))
    silent_max = float(wcfg.get("silent_max_min", 10))
    fuse = int(wcfg.get("fuse_tokens", 150_000))
    grace = float(wcfg.get("hard_interrupt_grace_sec", 30))

    while True:
        time.sleep(poll)
        st = wake_state.load(cfg)
        if not st.get("awake"):
            return 0  # cortex lay down on its own -> watchdog retires

        mt = transcript.mtime(cfg)
        silent_min = (time.time() - mt) / 60.0 if mt else 0.0
        tokens = transcript.window_tokens(cfg)

        # Publish the live NET spend (cache-miss rewrite + output) for the next
        # wake's Budget line; `tokens` (full occupancy) still drives fuse below.
        conn = db.connect(cfg)
        try:
            integration.store_window_tokens(conn, transcript.net_tokens(cfg))
        finally:
            conn.close()

        if fuse and tokens >= fuse:
            window.send_esc(cfg)
            note = _verify_esc_or_hard_interrupt(cfg, grace, "fuse")
            lie_down_mod.lie_down(cfg, force_slept="fuse" if not note else f"fuse {note}")
            return 0
        if silent_min >= silent_max:
            lie_down_mod.lie_down(cfg, force_slept="timeout")
            return 0


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    pidfile = wake_state.watchdog_pidfile_path(cfg)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))
    try:
        return run(cfg)
    finally:
        try:
            if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
