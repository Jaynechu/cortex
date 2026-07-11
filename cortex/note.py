"""Wakeup note: the床头字条 (bedside note) handed to a cortex session on wake.

gather() is the I/O layer — DB reads plus a best-effort macOS frontmost-app
probe. Every external source is wrapped in try/except so a failure omits its
line rather than crashing the wake. render() is pure — no I/O, no DB — so it
can be unit-tested with synthetic data.

Layout: a header block (Now / Plan Used / Active), then `---`-separated blocks
for pending self-schedule and Replay. The handoff injects at
SessionStart (marrow), not here. Cal/Rem lines retired (global inject pending).
The old "Wake:" reason line is gone — reasons carry no signal (desire engine
retired, wander-only).
"""
from __future__ import annotations

import json
import re
import subprocess
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cortex import config
from cortex.pacemaker.integration import parse_due_at
from cortex.pacemaker.integration import _today_tokens as _integration_today_tokens

# ct_rate_limit is a flat kv table (key, value, updated_at) the marrow-side
# collector owns (usage_snapshot). Keys read here: five_hour_pct /
# five_hour_reset_at, seven_day_pct / seven_day_reset_at, today_net_tokens.
# Any missing key -> that segment is omitted.
_FIVE_HOUR = ("five_hour_pct", "five_hour_reset_at")
_SEVEN_DAY = ("seven_day_pct", "seven_day_reset_at")
_TODAY_NET_KEY = "today_net_tokens"

# A wake row younger than this many seconds is treated as *this* wake (the tick
# logs it before the note is assembled), so "Last wake" reports the one before.
_CURRENT_WAKE_EPSILON_S = 90


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))


def _parse_utc(ts_iso: str) -> datetime:
    return datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))


def _local_hm(ts_iso: str, cfg: dict) -> str:
    return _parse_utc(ts_iso).astimezone(_tz(cfg)).strftime("%H:%M")


def _note_cfg(cfg: dict) -> dict:
    return cfg.get("note", {}) or {}


# Channels whose turns are cortex self-talk (wake monologues), excluded from
# Replay so the wakeup note does not replay itself back into its own context.
_DEFAULT_REPLAY_EXCLUDE_CHANNELS = ("ct",)


def _replay_exclude_channels(cfg: dict) -> tuple[str, ...]:
    raw = _note_cfg(cfg).get("replay_exclude_channels")
    if raw is None:
        return _DEFAULT_REPLAY_EXCLUDE_CHANNELS
    return tuple(str(c) for c in raw if str(c).strip())


# --------------------------------------------------------------------------- #
# DB-sourced facts
# --------------------------------------------------------------------------- #

def _last_wake(conn: sqlite3.Connection, now: datetime) -> dict | None:
    """Previous wake=1 row's age + force_slept marker. Skips a row logged for
    the current wake (younger than the epsilon)."""
    try:
        rows = conn.execute(
            "SELECT ts, force_slept FROM ct_wake_log WHERE wake = 1 "
            "ORDER BY ts DESC LIMIT 3"
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    for row in rows:
        try:
            age = now - _parse_utc(row["ts"])
        except (TypeError, ValueError):
            continue
        if age.total_seconds() < _CURRENT_WAKE_EPSILON_S:
            continue
        return {
            "minutes_ago": int(age.total_seconds() // 60),
            "force_slept": row["force_slept"],
        }
    return None


def _today_tokens(conn: sqlite3.Connection, now: datetime) -> int:
    """Cortex Today = sum of today's finished-window final occupancies + the
    current live window occupancy. Delegates to the daily-budget gate's helper
    (pacemaker.integration._today_tokens) so the note line and the gate always
    show the same figure (parity by construction, not by two copies)."""
    return _integration_today_tokens(conn, now)


def _rate_limit_kv(conn: sqlite3.Connection) -> dict:
    try:
        rows = conn.execute("SELECT key, value FROM ct_rate_limit").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["key"]: row["value"] for row in rows}


def _window_tokens(conn: sqlite3.Connection) -> int | None:
    """Window occupancy hint (statusline total: input + cache_read +
    cache_creation + output) published by lie_down / watchdog into
    ct_pacemaker_state JSON under 'window_tokens'. Absent -> None (segment
    omitted). Rendered as the Budget 'Net Session Token: Xk' segment."""
    try:
        row = conn.execute(
            "SELECT state FROM ct_pacemaker_state WHERE id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    try:
        state = json.loads(row["state"])
    except (ValueError, TypeError):
        return None
    val = state.get("window_tokens")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


# Media / bridge-marker shaper — deliberate copy of marrow strip_media_markers
# (marrow/marrow/transcript.py:102; marrow not importable from the cortex env).
# Keep byte-identical with that pairing. Strips wx [time:]/[sticker:] markers and
# <image|file|gif path="..."/> tags so replayed rows read like plain dialogue.
_TIME_PREFIX_RE = re.compile(r"^\[time:[^\]]+\]\s*")
_STICKER_LINE_RE = re.compile(r"^\[sticker:[^\]\n]*\]\n?", re.M)
_MEDIA_TAG_RE = re.compile(r'\s*<(?:image|file|gif)\s+path="[^"]*?"[^>]*>\s*')


def _strip_markers(text: str) -> str:
    if not text:
        return ""
    text = _TIME_PREFIX_RE.sub("", text)
    text = _STICKER_LINE_RE.sub("", text)
    return _MEDIA_TAG_RE.sub(" ", text).strip()


def _replay_events(conn: sqlite3.Connection, cfg: dict, limit: int, per_chars: int) -> list[dict]:
    """Last `limit` real user/assistant events (cross-session, chronological),
    each tagged [channel HH:mm] + role marker (N=user, Y=assistant) and capped
    at per_chars. Excludes role='tl' and cortex self-talk channels.

    Replay is meant to show the real user<->assistant exchange context; cortex's
    own wake monologues (channel='ct') are excluded so the note does not replay
    itself. The excluded channel set is config-driven (note.replay_exclude_channels)."""
    if limit <= 0:
        return []
    exclude = _replay_exclude_channels(cfg)
    placeholders = ",".join("?" for _ in exclude) if exclude else ""
    where_channel = (
        f" AND COALESCE(channel,'') NOT IN ({placeholders})" if exclude else "")
    try:
        rows = conn.execute(
            "SELECT role, content, timestamp, channel FROM events "
            "WHERE role IN ('user', 'assistant')" + where_channel
            + " ORDER BY id DESC LIMIT ?",
            (*exclude, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    events = []
    for row in reversed(rows):  # chronological
        ts = row["timestamp"]
        content = _strip_markers(row["content"])
        if not content:
            continue
        events.append({
            "channel": row["channel"] or "?",
            "hm": _local_hm(ts, cfg) if ts else "??:??",
            "role": "N" if row["role"] == "user" else "Y",
            "content": _truncate(content, per_chars),
        })
    return events


def _latest_replay_ts(conn: sqlite3.Connection, cfg: dict) -> str | None:
    """ISO timestamp of the most recent non-ct user/assistant event, or None."""
    exclude = _replay_exclude_channels(cfg)
    ph = ",".join("?" for _ in exclude) if exclude else ""
    where_ch = f" AND COALESCE(channel,'') NOT IN ({ph})" if exclude else ""
    try:
        row = conn.execute(
            "SELECT MAX(timestamp) as ts FROM events "
            "WHERE role IN ('user', 'assistant')" + where_ch,
            (*exclude,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["ts"] if row and row["ts"] else None


# --------------------------------------------------------------------------- #
# External best-effort facts (cadence CLI, osascript, handoff file)
# --------------------------------------------------------------------------- #

def _frontmost_app() -> str | None:
    """macOS frontmost application name. Locked screen / login window / any
    failure -> None (line omitted)."""
    try:
        out = subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to get name of first application '
             'process whose frontmost is true'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    name = out.stdout.strip()
    if out.returncode != 0 or not name or name in ("loginwindow",):
        return None
    return name


def _pending(cfg: dict, now: datetime) -> list[dict]:
    """self_schedule.json entries due within pending_window_min from now.
    due_at may be tz-aware or offset-free local ISO (parse_due_at handles
    both); a garbage/unparseable entry is skipped, never crashes the note."""
    path = config.self_schedule_path(cfg)
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    window = _note_cfg(cfg).get("pending_window_min", 15)
    horizon = now + timedelta(minutes=window)
    tz = _tz(cfg)
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            due = parse_due_at(item.get("due_at"), tz)
            if due is None or due > horizon:
                continue
            out.append({
                "hm": due.astimezone(tz).strftime("%H:%M"),
                "intent": item.get("intent", ""),
            })
        except (ValueError, TypeError):
            continue
    return out


# --------------------------------------------------------------------------- #
# gather / render
# --------------------------------------------------------------------------- #

def _safe(fn, *args, default=None):
    """Run one gather section; any exception -> default (section omitted).
    Belt-and-braces on top of each helper's own try/except: no data failure
    may crash note assembly (module design: best-effort throughout)."""
    try:
        return fn(*args)
    except Exception:
        return default


def gather(
    conn: sqlite3.Connection,
    cfg: dict,
    now: datetime,
    decision: dict | None = None,
    *,
    fresh: bool = False,
    wake_kind: str | None = None,
    died_no_handoff: bool = False,
) -> dict:
    """Assemble the wakeup note data dict. conn must use sqlite3.Row factory.
    `fresh`/`wake_kind` are accepted for caller compatibility; the handoff
    now injects at SessionStart, not here. `died_no_handoff` = the prior window
    crashed without a handoff (respawn catchup line)."""
    ncfg = _note_cfg(cfg)

    kv = _safe(_rate_limit_kv, conn, default={})
    budget = _safe(_build_budget, conn, cfg, now, kv, ncfg)
    last_wake = _safe(_last_wake, conn, now)
    replay = _safe(
        _replay_events, conn, cfg,
        ncfg.get("replay_events", 4),
        ncfg.get("replay_event_chars", 300),
        default=[],
    )
    replay_stale = False
    if replay and last_wake:
        latest_ts = _safe(_latest_replay_ts, conn, cfg)
        if latest_ts:
            try:
                last_wake_dt = now - timedelta(minutes=last_wake["minutes_ago"])
                replay_stale = _parse_utc(latest_ts) < last_wake_dt
            except (TypeError, ValueError):
                pass

    return {
        "last_wake": last_wake,
        "budget": budget,
        "active_app": _safe(_frontmost_app),
        "pending": _safe(_pending, cfg, now, default=[]),
        "died_no_handoff": died_no_handoff,
        "replay": replay,
        "replay_stale": replay_stale,
    }


def _build_budget(conn, cfg, now, kv, ncfg) -> dict:
    five = kv.get(_FIVE_HOUR[0])
    seven = kv.get(_SEVEN_DAY[0])
    five_reset = kv.get(_FIVE_HOUR[1])
    seven_reset = kv.get(_SEVEN_DAY[1])
    return {
        "five_h_pct": _as_float(five),
        "five_h_reset": _local_hm(five_reset, cfg) if five_reset else None,
        "seven_d_pct": _as_float(seven),
        "seven_d_countdown": _countdown(seven_reset, now) if seven_reset else None,
        "window_tokens": _window_tokens(conn),
        "today_tokens": _today_tokens(conn, now),
        "daily_budget": int(ncfg.get("daily_budget", 1_000_000)),
    }


def _countdown(reset_iso: str, now: datetime) -> str | None:
    """Remaining time until an ISO reset moment as a compact `1d2h`/`5h`/`12m`
    string. Past/unparseable -> None (segment omitted)."""
    try:
        delta = _parse_utc(reset_iso) - now.astimezone(ZoneInfo("UTC"))
    except (TypeError, ValueError):
        return None
    secs = int(delta.total_seconds())
    if secs <= 0:
        return None
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins = rem // 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def _as_float(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def render(cfg: dict, now: datetime, data: dict) -> str:
    """Pure assembly: data dict -> wakeup note text. No DB / no I/O.

    Layout (plan §一): a header block (Now / Plan Used / Active), then
    `---`-separated blocks for pending self-schedule and Replay, then a final
    turn-end reminder line (note.turn_end_text, every render; "" omits it).
    Handoff no longer lives here — it is injected at SessionStart on a
    fresh window."""
    header: list[str] = []

    now_seg = f"Now: {now.strftime('%H:%M %a')}"
    last = data.get("last_wake")
    if last:
        seg = f"Last wake: {last['minutes_ago']}min ago"
        # "auto" = routine proxy sleep on the silence path -> render neutrally,
        # never as a force incident. Only real force incidents get the tag.
        if last.get("force_slept") and last.get("force_slept") != "auto":
            seg += " (force-slept mid-task)"
        now_seg += f" | {seg}"
    header.append(now_seg)

    budget_line = _render_budget(data.get("budget"))
    if budget_line:
        header.append(budget_line)

    app = data.get("active_app")
    if app:
        header.append(f"Active (Mac): {app}")

    # Prior window was force-slept without writing its handoff -> tell this
    # window to backfill from DB events (recall/tl), never from raw jsonl.
    # "auto" (routine silence sleep) is not an incident -> no catchup line.
    if last and last.get("force_slept") and last.get("force_slept") != "auto":
        catchup = _note_cfg(cfg).get("force_slept_catchup_text", "")
        if catchup:
            header.append(catchup)

    # Prior window DIED (crash/manual close) mid-wake without a handoff -> the
    # fresh respawn recovers from its transcript.
    if data.get("died_no_handoff"):
        catchup = _note_cfg(cfg).get("died_no_handoff_catchup_text", "")
        if catchup:
            header.append(catchup)

    blocks: list[str] = ["\n".join(header)]

    pending = data.get("pending") or []
    if pending:
        segs = [f"due {p['hm']} {p['intent']}".rstrip() for p in pending]
        blocks.append("Pending self-schedule: " + " · ".join(segs))

    replay = data.get("replay") or []
    if data.get("replay_stale"):
        blocks.append("No new messages since last wake.")
    elif replay:
        rlines = ["### Replay"]
        for ev in replay:
            role = ev.get("role", "")
            content = " ".join((ev.get("content") or "").split())
            rlines.append(f"[{ev['channel']} {ev['hm']}] {role}: {content}")
        blocks.append("\n".join(rlines))

    note_text = "\n\n---\n\n".join(blocks)
    turn_end = _note_cfg(cfg).get("turn_end_text", "")
    if turn_end:
        note_text += "\n\n" + turn_end

    title = _note_cfg(cfg).get("title", "")
    if title:
        note_text = title + "\n\n" + note_text
    return note_text


def _render_budget(budget: dict | None) -> str | None:
    """Plan Used line — shows utilization (USED %, statusline口径), pipe-joined:
    `Plan Used: 5h 5% (04:50) | 7d 50% (1d2h) | Cortex Today 250k/1M 25% |
    Net Session Token: 50k`. Net Session Token is window occupancy (statusline
    total), not net spend — label kept for cross-system consistency with
    marrow's threshold line. Any missing datum drops just its segment."""
    if not budget:
        return None
    parts = []
    five = budget.get("five_h_pct")
    if five is not None:
        seg = f"5h {five:.0f}%"
        if budget.get("five_h_reset"):
            seg += f" ({budget['five_h_reset']})"
        parts.append(seg)
    seven = budget.get("seven_d_pct")
    if seven is not None:
        seg = f"7d {seven:.0f}%"
        if budget.get("seven_d_countdown"):
            seg += f" ({budget['seven_d_countdown']})"
        parts.append(seg)
    daily = int(budget.get("daily_budget", 1_000_000))
    today = int(budget.get("today_tokens", 0))
    pct = (today / daily * 100) if daily else 0
    parts.append(f"Cortex Today {today // 1000}k/{_fmt_budget(daily)} {pct:.0f}%")
    window = budget.get("window_tokens")
    if window is not None:
        parts.append(f"Net Session Token: {window // 1000}k")
    return "Plan Used: " + " | ".join(parts) if parts else None


def _fmt_budget(n: int) -> str:
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    return f"{n // 1000}k"


def _truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


if __name__ == "__main__":  # pragma: no cover - eyeball a real note
    from cortex import db

    _cfg = config.load()
    _conn = db.connect(_cfg)
    _now = datetime.now(_tz(_cfg))
    _data = gather(_conn, _cfg, _now, fresh=True, wake_kind="rotate")
    print(render(_cfg, _now, _data))
    _conn.close()
