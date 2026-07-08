"""Per-wake watchdog: spawned at note injection, killed at lie_down, never
resident. Every poll_sec it reads the transcript (mtime + window tokens) and
the awake marker, applying one-dog-three-judgements (plan 07-08):
  (a) running past run_max_min & still active -> esc + wrap-up nudge (self-wrap);
  (b) silent past silent_max_min without lie_down -> proxy lie_down (timeout);
  (c) window tokens >= fuse -> esc + proxy lie_down (fuse).
Three-layer trace: esc/inject -> ct_wake_log.force_slept -> next note's Last wake.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from cortex import config, db, transcript, wake_state, window
from cortex.pacemaker import integration

_WRAP_LINE = "写碎碎念收尾躺下"


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


def _elapsed_min(iso: str | None) -> float:
    try:
        t = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return 0.0
    return (datetime.now(timezone.utc) - t).total_seconds() / 60.0


def run(cfg: dict) -> int:
    from cortex import lie_down as lie_down_mod

    wcfg = cfg["wake"].get("watchdog", {})
    poll = int(wcfg.get("poll_sec", 60))
    run_max = float(wcfg.get("run_max_min", 10))
    silent_max = float(wcfg.get("silent_max_min", 5))
    fuse = int(wcfg.get("fuse_tokens", 150_000))
    wrap = cfg["wake"].get("wrap_line", _WRAP_LINE)

    wrap_sent = False
    while True:
        time.sleep(poll)
        st = wake_state.load(cfg)
        if not st.get("awake"):
            return 0  # cortex lay down on its own -> watchdog retires

        mt = transcript.mtime(cfg)
        silent_min = (time.time() - mt) / 60.0 if mt else 0.0
        tokens = transcript.window_tokens(cfg)
        run_min = _elapsed_min(st.get("awake_since"))

        # Publish the live token count for the next wake's Budget line.
        conn = db.connect(cfg)
        try:
            integration.store_window_tokens(conn, tokens)
        finally:
            conn.close()

        if fuse and tokens >= fuse:
            window.send_esc(cfg)
            lie_down_mod.lie_down(cfg, force_slept="fuse")
            return 0
        if silent_min >= silent_max:
            lie_down_mod.lie_down(cfg, force_slept="timeout")
            return 0
        if run_min >= run_max and not wrap_sent:
            window.send_esc(cfg)
            window.inject_line(cfg, wrap)
            wrap_sent = True  # give the self-wrap-up one chance, then let (b)/(c) act


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
