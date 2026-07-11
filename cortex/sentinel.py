"""cortex.sentinel — one-shot exact-time wake.

lie_down arms a detached sentinel (`python -m cortex.sentinel --seconds N`) that
sleeps until the next floor deadline, then runs one pacemaker tick — so the wake
fires on the second instead of up to 5 min late (the launchd 5-min tick stays as
a self-heal fallback: a sentinel lost to reboot/sleep is caught within 5 min).

Single detached process (start_new_session, like watchdog.spawn): killing a
predecessor pid never orphans a child sleep. Every new lie_down kills the
recorded predecessor before arming a fresh one; a user-wake reset kills it too.
On fire the sentinel clears its own sentinel_pid record — but only if the record
still matches its own pid (a newer lie_down may already have re-armed a different
one), the same self-guard as the watchdog pidfile.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from cortex import config, wake_state


def spawn(cfg: dict, seconds: float) -> int:
    """Launch a detached one-shot sentinel that wakes in `seconds`. Returns pid.
    Gated by [wake].sentinel; caller checks that before arming."""
    log = wake_state.watchdog_pidfile_path(cfg).with_suffix(".sentinel.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a")
    p = subprocess.Popen(
        [sys.executable, "-m", "cortex.sentinel", "--seconds", str(seconds)],
        stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ},
    )
    return p.pid


def run(cfg: dict, seconds: float) -> int:
    from cortex import pacemaker_tick

    if seconds > 0:
        time.sleep(seconds)
    # Clear our own record first (self-guarded) so a tick that decides to sleep
    # again re-arms cleanly; the tick's own gate handles awake short-circuit.
    wake_state.clear_sentinel_pid(cfg, only_if_pid=os.getpid())
    return pacemaker_tick.main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot exact-time wake: sleep then run a pacemaker tick")
    parser.add_argument("--seconds", type=float, required=True,
                        help="seconds to sleep before firing the tick")
    args = parser.parse_args(argv)
    cfg = config.load()
    return run(cfg, max(0.0, args.seconds))


if __name__ == "__main__":
    sys.exit(main())
