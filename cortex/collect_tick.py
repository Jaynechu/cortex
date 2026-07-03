"""Collector tick entry point (launchd, ~30min). Runs every collector once."""
from __future__ import annotations

import sys

from cortex import config, db
from cortex.collectors import run_all


def main() -> int:
    cfg = config.load()
    conn = db.connect(cfg)
    try:
        results = run_all(conn, cfg)
    finally:
        conn.close()
    ok = all(results.values())
    print(f"{db.utcnow_iso()} collect_tick {results}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
