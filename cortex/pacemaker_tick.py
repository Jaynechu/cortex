"""Pacemaker tick entry point (launchd, floor+jitter cadence). Log-only in
dry-run: records the wake decision, never sends. dry_run=false + wake=1 ->
calls the real cortex session (C3 wake runner)."""
from __future__ import annotations

import sys

from cortex import config, db
from cortex.pacemaker import integration
from cortex.wake import run_wake


def main() -> int:
    cfg = config.load()
    conn = db.connect(cfg)
    try:
        decision = integration.run_tick(conn, cfg)
        dry_run = bool(cfg["pacemaker"].get("dry_run", True))
        if decision["wake"] and not dry_run:
            run_wake(conn, cfg, decision)
    finally:
        conn.close()
    print(f"{db.utcnow_iso()} {decision['explanation']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
