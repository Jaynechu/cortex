"""cortex.wait — declare a one-shot silence window for the per-wake watchdog.

Normally the watchdog proxies a lie-down once the transcript has been silent
for silent_max_min. When the resident session expects a lull (e.g. waiting for
the user to come back), it can declare "hold off for X minutes": the watchdog
holds its routine timeout until the deadline, then resets to the default — the
extension fires only once, and X is clamped to the wake-window max (cache-TTL
guard). The runaway fuse (token cap) is unaffected.

Usage: python -m cortex.wait --minutes 30
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from cortex import config, wake_state
from cortex.pacemaker.triggers import clamp_window_minutes


def wait(cfg: dict, minutes: float) -> dict:
    minutes = clamp_window_minutes(minutes, cfg)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    # One atomic strict-locked mutation: verify awake + wait quota not spent this
    # round (F5: no consecutive empty waits — any activity clears the flag first),
    # bump gen (an accepted wait re-arms the silence window = a new cancellation
    # epoch), set silence_wait_until, mark wait_spent, clear tuck_pending.
    res = wake_state.commit_wait(cfg, until.isoformat())
    if not res.get("ok"):
        return {"ok": False, "refused": True, "reason":
                "No consecutive waits - act (any tool) then wait, or lie_down."}
    return {"ok": True, "minutes": minutes, "until": until.isoformat()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Hold off the watchdog's routine timeout for X minutes")
    parser.add_argument("--minutes", type=float, required=True,
                        help="minutes to stay awake-idle (clamped to the wake window)")
    args = parser.parse_args(argv)
    cfg = config.load()
    result = wait(cfg, args.minutes)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
