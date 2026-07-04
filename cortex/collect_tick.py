"""Collector tick entry point (launchd, ~30min). Runs every collector once,
then re-renders day_log.md so it stays fresh between wakes."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

from cortex import config, day_log, db
from cortex.collectors import run_all

_USAGE_SNAPSHOT_TIMEOUT_S = 15


def _run_usage_snapshot(conn, cfg: dict) -> None:
    """Spawn marrow's own venv python to run `python -m marrow.usage_snapshot`
    (OAuth usage % -> ct_rate_limit kv). Same own-venv-subprocess pattern as
    wake.py:call_marrow_cortex (marrow has its own deps, cortex never imports
    it in-process). Best-effort: never raises, never blocks the tick — logs
    failure to ct_collector_log like the other collectors."""
    if not cfg.get("tick", {}).get("usage_snapshot", True):
        return
    python = os.path.expanduser(cfg["marrow"]["venv_python"])
    try:
        proc = subprocess.run(
            [python, "-m", "marrow.usage_snapshot"],
            capture_output=True, text=True, timeout=_USAGE_SNAPSHOT_TIMEOUT_S,
        )
        ok = proc.returncode == 0
        error = None if ok else proc.stderr.strip()[-2000:]
    except Exception as exc:  # noqa: BLE001 - must not kill the tick
        ok = False
        error = str(exc)
    db.log_collector_run(conn, "usage", ok=ok, error=error)


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
        _run_usage_snapshot(conn, cfg)
        _render_day_log(conn, cfg)
    finally:
        conn.close()
    ok = all(results.values())
    print(f"{db.utcnow_iso()} collect_tick {results}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
