"""Collector registry + run_all entry point.

Each collector is idempotent (safe to re-run) and writes its own ct_ rows.
A failing collector must not stop the others; failures are logged to
ct_collector_log and returned to the caller.
"""
from __future__ import annotations

import sqlite3
import traceback

from cortex import db
from cortex.collectors import geofence, health, knowledgec

COLLECTORS = {
    "knowledgec": knowledgec.collect,
    "geofence": geofence.collect,
    "health": health.collect,
}


def run_all(conn: sqlite3.Connection, cfg: dict) -> dict[str, bool]:
    """Run every collector, catching errors per-source. Returns {source: ok}."""
    results: dict[str, bool] = {}
    for source, fn in COLLECTORS.items():
        try:
            fn(conn, cfg)
            db.log_collector_run(conn, source, ok=True)
            results[source] = True
        except Exception as exc:  # noqa: BLE001 - collectors must not kill each other
            error = f"{exc}\n{traceback.format_exc()}"
            db.log_collector_run(conn, source, ok=False, error=error)
            results[source] = False
    return results
