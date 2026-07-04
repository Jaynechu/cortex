"""Collector tick entry point (launchd, ~30min). Runs every collector once,
then re-renders day_log.md so it stays fresh between wakes."""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from cortex import config, day_log, db
from cortex.collectors import run_all


def _render_day_log(conn, cfg: dict) -> None:
    """Re-render Status/Flow/Tasks/Track between wakes. Tolerant: day_log.md
    missing -> skip quietly (wake owns creation/archive lifecycle; the tick
    path never creates or archives it)."""
    path = config.day_log_path(cfg)
    if not path.exists():
        return
    day_log.update(path, conn, cfg, datetime.now(timezone.utc))


def main() -> int:
    cfg = config.load()
    conn = db.connect(cfg)
    try:
        results = run_all(conn, cfg)
        _render_day_log(conn, cfg)
    finally:
        conn.close()
    ok = all(results.values())
    print(f"{db.utcnow_iso()} collect_tick {results}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
