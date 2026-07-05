"""Pacemaker tick entry point (launchd, floor+jitter cadence). Log-only in
dry-run: records the wake decision, never sends. dry_run=false + wake=1 ->
calls the real cortex session (C3 wake runner)."""
from __future__ import annotations

import sys
import time

from cortex import config, db
from cortex.pacemaker import integration
from cortex.wake import run_wake


def main() -> int:
    cfg = config.load()
    conn = db.connect(cfg)
    try:
        t_tick = time.monotonic()
        decision = integration.run_tick(conn, cfg)
        t_gate = time.monotonic()
        dry_run = bool(cfg["pacemaker"].get("dry_run", True))
        if decision["wake"]:
            try:
                if not dry_run:
                    run_wake(conn, cfg, decision, tick_started=t_tick, gate_done=t_gate)
            finally:
                # Lie down even on wake failure — the floor clock restarts
                # here (C-wm) so a crashed wake can't wedge the heartbeat.
                integration.lie_down(conn, cfg)
    finally:
        conn.close()
    print(f"{db.utcnow_iso()} {decision['explanation']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
