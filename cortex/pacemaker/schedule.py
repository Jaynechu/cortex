"""Schedule (duty) triggers: fixed recurring duties, explicit and rare. Each
duty is a config [[schedule]] block (name, time "HH:MM" local, optional
prompt_path, enabled). A duty fires on the first tick at/after its local time
each day, once per day; `fired` (name -> last-fired date) dedups within the day.

Pure due-computation here (testable). Persistence of `fired` + window spawn live
in the integration/wake layers. Disabled duties (enabled=false) never fire.
"""
from __future__ import annotations

from datetime import datetime, time


def _parse_hhmm(value: str) -> time | None:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def due_duties(entries: list, now: datetime, fired: dict) -> list[dict]:
    """Duties whose local time is at/past `now` today and not yet fired today.
    `entries` = config [[schedule]] blocks; `fired` = {name: "YYYY-MM-DD"}.
    Each result carries name + prompt_path so downstream spawns a fresh window."""
    today = now.date().isoformat()
    now_t = now.timetz().replace(tzinfo=None)
    out: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        name = entry.get("name")
        t = _parse_hhmm(entry.get("time", ""))
        if not name or t is None:
            continue
        if now_t < t:
            continue
        if fired.get(name) == today:
            continue
        out.append({"name": name, "prompt_path": entry.get("prompt_path") or None})
    return out
