"""day_log.md renderer + updater: v2 zones (Decided 07-03 eve, frozen at C3).

Zone First    — cortex-written 3-5 action lines, preserved byte-for-byte on
  re-render (like Notes); cortex overwrites it herself during a wake.
Zone Status   — render-only snapshot, rewritten every update().
Zone Flow     — (display title, was "Today") one time axis: geofence auto
  rows + tl rows (events role='tl'), re-sorted by their leading HH:mm so a
  late tl write self-heals. Pure DB->render, one-way (no reconcile — Decided
  07-03 eve HARD). Calendar rows are not wired yet (schedule.py ownership
  moves here at C6) — omitted, not faked.
Zone Tasks    — (display title, was "Reminders") task pool, not nag
  triggers — coax only. Placeholder until a reminder collector exists (tail
  block).
Zone Track    — placeholder until category-bucket config + sleep inference
  land; screentime/geofence data exists but bucket definitions do not.

Renames (07-04 Decided): Today->Flow, Reminders->Tasks are DISPLAY TITLE
ONLY — the HTML-comment zone marker IDs below (TODAY_START/END,
REMINDERS_START/END) and the python constant names are unchanged on
purpose, so existing day_log.md files and any marker-based reader survive
the rename untouched.
Zone Notes — cortex free text; everything after the marker is
  carried over byte-for-byte on re-render, so hand edits are never clobbered.

Zone boundaries are stable HTML comment markers so the file survives
round-trips even if the surrounding prose changes.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

FIRST_START = "<!-- cortex:first:start -->"
FIRST_END = "<!-- cortex:first:end -->"
STATUS_START = "<!-- cortex:status:start -->"
STATUS_END = "<!-- cortex:status:end -->"
TODAY_START = "<!-- cortex:today:start -->"
TODAY_END = "<!-- cortex:today:end -->"
REMINDERS_START = "<!-- cortex:reminders:start -->"
REMINDERS_END = "<!-- cortex:reminders:end -->"
TRACK_START = "<!-- cortex:track:start -->"
TRACK_END = "<!-- cortex:track:end -->"
NOTES_START = "<!-- cortex:notes:start -->"

DEFAULT_FIRST_BODY = "## First\n(pending first wake)"
DEFAULT_STATUS_BODY = "## Status\n(pending first update)"
DEFAULT_TODAY_BODY = "## Flow\n(no rows yet today)"
DEFAULT_REMINDERS_BODY = (
    "## Tasks\n"
    "task pool, not nag triggers — coax only\n"
    "(no reminder data collected yet)"
)
DEFAULT_TRACK_BODY = "## Track\n(pending category-bucket + sleep inference wiring)"
DEFAULT_NOTES_BODY = "## Notes\n"

_HM_PREFIX = re.compile(r"^(\d{2}:\d{2})")


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))


def _local_hm(ts_iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return dt.astimezone(tz).strftime("%H:%M")


def _utc_day_bounds(now: datetime, tz: ZoneInfo) -> tuple[str, str]:
    """[start, end) UTC 'Z' bounds for the local calendar day containing `now`.
    DB timestamps (ct_activity.ts, events.ts_start) are stored UTC; the local
    Melbourne day maps to a UTC window, so a naive local-date prefix match
    (ts LIKE 'YYYY-MM-DD%') would misfile the pre-10:00 rows whose UTC date is
    still the previous day. Both columns share the '...SSZ' fixed format, so
    string range comparison against these bounds is exact."""
    start_local = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start_utc.strftime(fmt), end_utc.strftime(fmt)


def _last_seen_line(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> str:
    start, end = _utc_day_bounds(now, tz)
    row = conn.execute(
        "SELECT ts, channel FROM ct_activity WHERE ts >= ? AND ts < ? ORDER BY ts DESC LIMIT 1",
        (start, end),
    ).fetchone()
    if row is None:
        return "no activity today"
    return f"{_local_hm(row['ts'], tz)} {row['channel']}"


def _usage_line(conn: sqlite3.Connection, date: str) -> str:
    row = conn.execute(
        "SELECT category, seconds FROM ct_category_usage WHERE date = ? ORDER BY seconds DESC LIMIT 1",
        (date,),
    ).fetchone()
    if row is None:
        return "no usage data"
    hours = row["seconds"] / 3600
    return f"{row['category']} {hours:.1f}h (top)"


def _collector_lines(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT source, ts, ok, error FROM ct_collector_log "
        "WHERE id IN (SELECT MAX(id) FROM ct_collector_log GROUP BY source) "
        "ORDER BY source"
    ).fetchall()
    if not rows:
        return ["no runs logged yet"]
    lines = []
    for row in rows:
        status = "ok" if row["ok"] else f"FAILED ({row['error']})"
        lines.append(f"{row['source']}: {status} @ {row['ts']}")
    return lines


def render_status(conn: sqlite3.Connection, cfg: dict, now: datetime) -> str:
    date = now.date().isoformat()
    tz = _tz(cfg)
    lines = [
        "## Status",
        f"Last seen: {_last_seen_line(conn, now, tz)}",
        f"Usage today: {_usage_line(conn, date)}",
        "Collectors: " + " · ".join(_collector_lines(conn)),
    ]
    return "\n".join(lines)


def _sort_key(line: str) -> str:
    m = _HM_PREFIX.match(line)
    return m.group(1) if m else "99:99"


def _geofence_rows_today(conn: sqlite3.Connection, date: str) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT time, event FROM ct_geofence WHERE date = ? ORDER BY time", (date,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [f"{row['time']} [{row['event']}]" for row in rows]


def _tl_rows_today(conn: sqlite3.Connection, now: datetime, tz: ZoneInfo) -> list[str]:
    """tl rows = events role='tl' (A2r): content holds only 【label】body,
    the HH:mm-HH:mm range lives in ts_start/ts_end — rebuild the display
    line here. One-way DB->render (no reconcile — Decided 07-03 eve HARD;
    reconcile-first ordering is a C4 concern). ts_start is UTC, so filter on
    the local-day UTC window rather than a local-date prefix."""
    start, end = _utc_day_bounds(now, tz)
    rows = conn.execute(
        "SELECT ts_start, ts_end, content FROM events WHERE role = 'tl' "
        "AND ts_start >= ? AND ts_start < ? ORDER BY ts_start ASC",
        (start, end),
    ).fetchall()
    lines = []
    for row in rows:
        start_hm = _local_hm(row["ts_start"], tz)
        stamp = f"{start_hm}-{_local_hm(row['ts_end'], tz)}" if row["ts_end"] else start_hm
        lines.append(f"{stamp} {row['content']}")
    return lines


def render_today(conn: sqlite3.Connection, cfg: dict, now: datetime) -> str:
    date = now.date().isoformat()
    tz = _tz(cfg)
    lines = _geofence_rows_today(conn, date) + _tl_rows_today(conn, now, tz)
    lines.sort(key=_sort_key)
    body = "\n".join(lines) if lines else "(no rows yet today)"
    return "## Flow\n" + body


def render_reminders(conn: sqlite3.Connection, cfg: dict, now: datetime) -> str:
    """Tasks zone (display title, was Reminders) — task pool, not nag
    triggers, coax only. No reminder collector exists yet (rem automation =
    tail block, her rules first). Honest placeholder until due/overdue/done
    wiring lands."""
    return DEFAULT_REMINDERS_BODY


def render_track(conn: sqlite3.Connection, cfg: dict, now: datetime) -> str:
    """Screentime (ct_category_usage) and geofence data exist, but the
    Focus G/U/O/Code bucket mapping and sleep inference are undefined —
    honest placeholder rather than fabricated numbers."""
    return DEFAULT_TRACK_BODY


def _extract_bounded(existing_text: str | None, start: str, end: str, default_body: str) -> str:
    """Return the text between start/end markers verbatim, or default_body
    if absent (new file / corrupted markers)."""
    if not existing_text:
        return default_body
    s = existing_text.find(start)
    e = existing_text.find(end)
    if s == -1 or e == -1 or e < s:
        return default_body
    body = existing_text[s + len(start): e].strip("\n")
    return body or default_body


def _split_notes(existing_text: str | None) -> str:
    """Return the free-text tail after the Notes marker, byte-for-byte, or
    '' if the marker is absent (new file / corrupted marker)."""
    if not existing_text:
        return ""
    marker_line = NOTES_START + "\n"
    idx = existing_text.find(marker_line)
    if idx == -1:
        return ""
    return existing_text[idx + len(marker_line):]


def render_day_log(
    conn: sqlite3.Connection, cfg: dict, now: datetime, existing_text: str | None = None
) -> str:
    """Assemble the full file: First carried over verbatim (cortex's own
    writing), Status/Today/Reminders/Track rebuilt fresh, Notes carried over
    verbatim from existing_text (or fresh defaults)."""
    date = now.date().isoformat()
    first_body = _extract_bounded(existing_text, FIRST_START, FIRST_END, DEFAULT_FIRST_BODY)
    head_lines = [
        date,
        "",
        FIRST_START,
        first_body,
        FIRST_END,
        "",
        STATUS_START,
        render_status(conn, cfg, now),
        STATUS_END,
        "",
        TODAY_START,
        render_today(conn, cfg, now),
        TODAY_END,
        "",
        REMINDERS_START,
        render_reminders(conn, cfg, now),
        REMINDERS_END,
        "",
        TRACK_START,
        render_track(conn, cfg, now),
        TRACK_END,
        "",
    ]
    head = "\n".join(head_lines) + "\n"
    notes_tail = _split_notes(existing_text) or DEFAULT_NOTES_BODY
    return head + NOTES_START + "\n" + notes_tail


def update(path: Path, conn: sqlite3.Connection, cfg: dict, now: datetime) -> None:
    """Re-read the file (if present) for First/Notes preservation, rebuild
    Status/Today/Reminders/Track, and write the result back."""
    existing_text = path.read_text() if path.exists() else None
    text = render_day_log(conn, cfg, now, existing_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def new_day(path: Path, date: str) -> None:
    """Create a fresh day_log file for `date` with empty zones. Caller must
    archive() the previous file first — this always overwrites."""
    head_lines = [
        date,
        "",
        FIRST_START,
        DEFAULT_FIRST_BODY,
        FIRST_END,
        "",
        STATUS_START,
        DEFAULT_STATUS_BODY,
        STATUS_END,
        "",
        TODAY_START,
        DEFAULT_TODAY_BODY,
        TODAY_END,
        "",
        REMINDERS_START,
        DEFAULT_REMINDERS_BODY,
        REMINDERS_END,
        "",
        TRACK_START,
        DEFAULT_TRACK_BODY,
        TRACK_END,
        "",
    ]
    text = "\n".join(head_lines) + "\n" + NOTES_START + "\n" + DEFAULT_NOTES_BODY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def archive(path: Path, archive_dir: Path) -> Path:
    """Move `path` into `archive_dir` as-is (no compression), named after
    the file's L1 date header."""
    if not path.exists():
        raise FileNotFoundError(f"day_log not found: {path}")
    text = path.read_text()
    first_line = text.splitlines()[0].strip() if text.strip() else "unknown"
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{first_line}.md"
    if dest.exists():
        # Never clobber an existing archive: a same-day re-archive (e.g. a
        # failed wake retry) must not overwrite the real data with a blank
        # shell. Fall back to the next free -N suffix.
        i = 2
        while (dest := archive_dir / f"{first_line}-{i}.md").exists():
            i += 1
    shutil.move(str(path), str(dest))
    return dest
