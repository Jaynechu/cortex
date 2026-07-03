"""Pacemaker tick entry point (launchd, floor+jitter cadence). Log-only in
dry-run: records the wake decision, never sends (no outbound until C5)."""
from __future__ import annotations

import sys

from cortex import config, db
from cortex.pacemaker import integration


def main() -> int:
    cfg = config.load()
    conn = db.connect(cfg)
    try:
        decision = integration.run_tick(conn, cfg)
    finally:
        conn.close()
    print(f"{db.utcnow_iso()} {decision['explanation']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
