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


def spawn(cfg: dict, seconds: float, gen: int | None = None,
          state_id: str | None = None, target_iso: str | None = None) -> int:
    """Launch a detached one-shot sentinel that wakes in `seconds`. Returns pid.
    Gated by [wake].sentinel; caller checks that before arming. The (gen,
    state_id, target) it fires against are baked in as CLI args so the fire-time
    check needs no ambient state — a stale epoch (a lie_down / user reset that
    bumped gen after this spawn) is caught even if the pidfile was already
    overwritten."""
    log = wake_state.watchdog_pidfile_path(cfg).with_suffix(".sentinel.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a")
    argv = [sys.executable, "-m", "cortex.sentinel", "--seconds", str(seconds)]
    if gen is not None:
        argv += ["--gen", str(gen)]
    if state_id is not None:
        argv += ["--state-id", str(state_id)]
    if target_iso is not None:
        argv += ["--target", str(target_iso)]
    p = subprocess.Popen(
        argv, stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ},
    )
    return p.pid


def run(cfg: dict, seconds: float, gen: int | None = None,
        state_id: str | None = None, target_iso: str | None = None) -> int:
    from cortex import pacemaker_tick

    if seconds > 0:
        time.sleep(seconds)
    # Fire-time epoch check: verify the (gen, state_id) this sentinel was armed
    # for is STILL the live epoch. A newer lie_down / user reset since arm time
    # bumped gen -> this alarm belongs to a cancelled epoch; exit silently,
    # writing/clearing nothing that is not our own. Fail closed: a lock/parse
    # failure also aborts (never fire on a doubtful read).
    if gen is not None:
        try:
            if not wake_state.token_current(cfg, (gen, state_id)):
                return 0
        except wake_state.StateValidationError:
            return 0
    # Ledger cross-check: the durable next_wake_at must still point at our target
    # (a newer lie_down may have re-armed a different time under the same gen path
    # via ctl; guard against a stale spawn firing early). Skip when not supplied.
    if target_iso is not None:
        current = wake_state.get_next_wake_at(cfg)
        if current is not None and str(current) != str(target_iso):
            return 0
    # Clear our own record first (self-guarded) so a tick that decides to sleep
    # again re-arms cleanly; the tick's own gate handles awake short-circuit.
    wake_state.clear_sentinel_pid(cfg, only_if_pid=os.getpid())
    return pacemaker_tick.main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot exact-time wake: sleep then run a pacemaker tick")
    parser.add_argument("--seconds", type=float, required=True,
                        help="seconds to sleep before firing the tick")
    parser.add_argument("--gen", type=int, default=None,
                        help="cancellation epoch this alarm was armed for")
    parser.add_argument("--state-id", default=None,
                        help="state_id this alarm was armed for (ABA guard)")
    parser.add_argument("--target", default=None,
                        help="target next_wake_at ISO this alarm was armed for")
    args = parser.parse_args(argv)
    cfg = config.load()
    return run(cfg, max(0.0, args.seconds), gen=args.gen,
               state_id=args.state_id, target_iso=args.target)


if __name__ == "__main__":
    sys.exit(main())
