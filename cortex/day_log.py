"""day_log.md renderer + updater: three zones per Frame (day-throwaway
derived view, history lives in DB, one file per day, archived at rebirth).

Zone Status  — render-only snapshot, rewritten every update().
Zone Timeline — today's session_digests life_lines, read-only display; the
  existing marrow reconcile machinery owns edits, this module never writes
  timeline content back to the DB.
Zone Notes — cortex free text; everything after the Notes marker is carried
  over byte-for-byte on re-render, so her edits are never clobbered.

Zone boundaries are stable HTML comment markers so the file survives
round-trips even if the surrounding prose changes.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

STATUS_START = "<!-- cortex:status:start -->"
STATUS_END = "<!-- cortex:status:end -->"
TIMELINE_START = "<!-- cortex:timeline:start -->"
TIMELINE_END = "<!-- cortex:timeline:end -->"
NOTES_START = "<!-- cortex:notes:start -->"

DEFAULT_STATUS_BODY = "## Status\n(pending first update)"
DEFAULT_TIMELINE_BODY = "## Timeline\n(no entries yet today)"
DEFAULT_NOTES_BODY = "## Notes\n"


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))


def _local_hm(ts_iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    return dt.astimezone(tz).strftime("%H:%M")


def _last_seen_line(conn: sqlite3.Connection, date: str, tz: ZoneInfo) -> str:
    row = conn.execute(
        "SELECT ts, channel FROM ct_activity WHERE ts LIKE ? ORDER BY ts DESC LIMIT 1",
        (f"{date}%",),
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
        f"Last seen: {_last_seen_line(conn, date, tz)}",
        f"Usage today: {_usage_line(conn, date)}",
        "Collectors: " + " · ".join(_collector_lines(conn)),
    ]
    return "\n".join(lines)


def render_timeline(conn: sqlite3.Connection, now: datetime) -> str:
    date = now.date().isoformat()
    rows = conn.execute(
        "SELECT life_lines FROM session_digests "
        "WHERE date = ? AND life_lines IS NOT NULL AND life_lines != '' "
        "ORDER BY ts ASC",
        (date,),
    ).fetchall()
    if not rows:
        return DEFAULT_TIMELINE_BODY
    lines = ["## Timeline"]
    lines.extend(row["life_lines"] for row in rows)
    return "\n".join(lines)


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
    """Assemble the full file: Status + Timeline rebuilt fresh, Notes carried
    over verbatim from existing_text (or a fresh default block)."""
    date = now.date().isoformat()
    head_lines = [
        date,
        "",
        STATUS_START,
        render_status(conn, cfg, now),
        STATUS_END,
        "",
        TIMELINE_START,
        render_timeline(conn, now),
        TIMELINE_END,
        "",
    ]
    head = "\n".join(head_lines) + "\n"
    notes_tail = _split_notes(existing_text) or DEFAULT_NOTES_BODY
    return head + NOTES_START + "\n" + notes_tail


def update(path: Path, conn: sqlite3.Connection, cfg: dict, now: datetime) -> None:
    """Re-read the file (if present) for Notes preservation, rebuild Status
    and Timeline, and write the result back."""
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
        STATUS_START,
        DEFAULT_STATUS_BODY,
        STATUS_END,
        "",
        TIMELINE_START,
        DEFAULT_TIMELINE_BODY,
        TIMELINE_END,
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
    shutil.move(str(path), str(dest))
    return dest
