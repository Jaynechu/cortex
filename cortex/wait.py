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
    cap = int(cfg["wake"].get("wait_max_per_wake", 2) or 0)
    used = wake_state.get_wait_count(cfg)
    if cap > 0 and used >= cap:
        return {"ok": False, "refused": True, "wait_count": used, "cap": cap,
                "reason": f"wait() cap reached ({used}/{cap} this wake) — "
                          "lie_down or do nothing (10min silence auto-sleeps)."}
    minutes = clamp_window_minutes(minutes, cfg)
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    wake_state.set_wait_until(cfg, until.isoformat())
    count = wake_state.bump_wait_count(cfg)
    return {"ok": True, "minutes": minutes, "until": until.isoformat(),
            "wait_count": count, "cap": cap}


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
