"""Wakeup note: the床头字条 (bedside note) handed to a cortex session on wake.

gather() is the I/O layer — DB reads plus best-effort external facts (cadence
CLI calendar/reminders, macOS frontmost app, handoff file). Every external
source is a module-level helper wrapped in try/except so a missing tool or a
locked screen omits its line rather than crashing the wake. render() is pure —
no I/O, no DB — so it can be unit-tested with synthetic data.

Language: English labels throughout; the only localized text is the handoff
note content and the two section titles (both configurable — persona strings
live in config, never hardcoded here).
"""
from __future__ import annotations

import json
import subprocess
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from cortex import config
from cortex.pacemaker.integration import parse_due_at

# ct_rate_limit is a flat kv table (key, value, updated_at) the marrow-side
# writer owns (parses rate_limit_event off the cortex claude stream). Keys used
# here: five_hour_pct / five_hour_reset_at, seven_day_pct. Missing -> omitted.
_FIVE_HOUR = ("five_hour_pct", "five_hour_reset_at")
_SEVEN_DAY = ("seven_day_pct", None)

# A wake row younger than this many seconds is treated as *this* wake (the tick
# logs it before the note is assembled), so "Last wake" reports the one before.
_CURRENT_WAKE_EPSILON_S = 90


def _tz(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))


def _parse_utc(ts_iso: str) -> datetime:
    return datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))


def _local_hm(ts_iso: str, cfg: dict) -> str:
    return _parse_utc(ts_iso).astimezone(_tz(cfg)).strftime("%H:%M")


def _bulletin_cfg(cfg: dict) -> dict:
    return cfg.get("bulletin", {}) or {}


def _note_cfg(cfg: dict) -> dict:
    return cfg.get("note", {}) or {}


# --------------------------------------------------------------------------- #
# Wake line
# --------------------------------------------------------------------------- #

def _reason_kind_detail(reason) -> tuple[str, str, dict]:
    if isinstance(reason, dict):
        return reason.get("kind", ""), reason.get("detail", ""), reason.get("facts", {}) or {}
    return (
        getattr(reason, "kind", ""),
        getattr(reason, "detail", ""),
        getattr(reason, "facts", {}) or {},
    )


def _wake_parts(decision: dict | None) -> list[str]:
    """Map decision reasons to display fragments (plan §wakeup note):
    floor -> 巡回, self_scheduled -> Self-schedule(<intent>),
    schedule -> Schedule(<name>). Unknown kinds fall back to their detail."""
    if not decision:
        return ["巡回"]
    parts: list[str] = []
    for reason in decision.get("reasons", []) or []:
        kind, detail, facts = _reason_kind_detail(reason)
        if kind == "floor":
            parts.append("巡回")
        elif kind == "self_scheduled":
            parts.append(f"Self-schedule({facts.get('intent') or detail})")
        elif kind == "schedule":
            parts.append(f"Schedule({facts.get('name') or detail})")
        elif detail:
            parts.append(detail)
    if parts:
        return parts
    explanation = decision.get("explanation")
    return [explanation] if explanation else ["巡回"]


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
    """SUM(ct_wake_log.tokens) for today's Melbourne-local date. ts is UTC ISO,
    so filter from local midnight converted to UTC then compare local dates."""
    start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(ZoneInfo("UTC")).isoformat()
    try:
        rows = conn.execute(
            "SELECT ts, tokens FROM ct_wake_log WHERE tokens IS NOT NULL AND ts >= ?",
            (start_utc,),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0
    total = 0
    today = now.date()
    tz = now.tzinfo
    for row in rows:
        try:
            if _parse_utc(row["ts"]).astimezone(tz).date() == today:
                total += int(row["tokens"])
        except (TypeError, ValueError):
            continue
    return total


def _rate_limit_kv(conn: sqlite3.Connection) -> dict:
    try:
        rows = conn.execute("SELECT key, value FROM ct_rate_limit").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["key"]: row["value"] for row in rows}


def _window_tokens(conn: sqlite3.Connection) -> int | None:
    """NET spend hint (cache-miss rewrite + output) published by lie_down /
    watchdog into ct_pacemaker_state JSON under 'window_tokens'. Absent -> None
    (segment omitted). Rendered as the Budget 'net Xk' segment."""
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


def _replay_events(conn: sqlite3.Connection, cfg: dict, limit: int, per_chars: int) -> list[dict]:
    """Last `limit` user/assistant events (cross-session, chronological), each
    tagged [channel HH:mm] and capped at per_chars. Excludes role='tl'."""
    if limit <= 0:
        return []
    try:
        rows = conn.execute(
            "SELECT role, content, timestamp, channel FROM events "
            "WHERE role IN ('user', 'assistant') ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    events = []
    for row in reversed(rows):  # chronological
        ts = row["timestamp"]
        events.append({
            "channel": row["channel"] or "?",
            "hm": _local_hm(ts, cfg) if ts else "??:??",
            "content": _truncate(row["content"], per_chars),
        })
    return events


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


def _cadence_json(cfg: dict, args: list[str]) -> list | None:
    binp = config.cadence_bin_path(cfg)
    if not binp.exists():
        return None
    try:
        out = subprocess.run(
            [str(binp), *args, "--json"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def _cal_line(cfg: dict, now: datetime) -> dict | None:
    """Current timed event (start<=now<=end) + next timed event today. All-day
    entries are skipped (they'd always read as 'current')."""
    data = _cadence_json(cfg, ["cal", "read", now.date().isoformat()])
    if data is None:
        return None
    current = None
    nxt = None
    nxt_start = None
    for ev in data:
        if ev.get("all_day"):
            continue
        title = ev.get("title") or ev.get("summary")
        try:
            start = datetime.fromisoformat(ev["start"])
            end = datetime.fromisoformat(ev["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if start <= now <= end and current is None:
            current = title
        elif start > now and (nxt_start is None or start < nxt_start):
            nxt, nxt_start = title, start
    if current is None and nxt is None:
        return None
    return {"current": current, "next": nxt}


def _rem_last_done(cfg: dict) -> str | None:
    """Title of the most recently completed reminder."""
    data = _cadence_json(cfg, ["rem", "read", "--done"])
    if not data:
        return None
    best = None
    best_key = None
    for rem in data:
        key = rem.get("completion_date")
        if key and (best_key is None or key > best_key):
            best, best_key = rem.get("title"), key
    return best


def _read_handoff(cfg: dict, fresh: bool, wake_kind: str | None) -> str | None:
    """Handoff note content — only on a fresh window whose wake kind opts in."""
    if not fresh:
        return None
    kinds = _note_cfg(cfg).get("handoff_wake_kinds", [])
    if wake_kind is not None and wake_kind not in kinds:
        return None
    path = config.handoff_path(cfg)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


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
    window = _bulletin_cfg(cfg).get("pending_window_min", 15)
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
) -> dict:
    """Assemble the wakeup note data dict. conn must use sqlite3.Row factory.
    `fresh`/`wake_kind` gate the handoff section (fresh windows only)."""
    bcfg = _bulletin_cfg(cfg)
    ncfg = _note_cfg(cfg)

    kv = _safe(_rate_limit_kv, conn, default={})
    budget = _safe(_build_budget, conn, cfg, now, kv, bcfg)

    return {
        "wake_parts": _safe(_wake_parts, decision, default=["巡回"]),
        "last_wake": _safe(_last_wake, conn, now),
        "budget": budget,
        "active_app": _safe(_frontmost_app),
        "cal": _safe(_cal_line, cfg, now),
        "rem_last_done": _safe(_rem_last_done, cfg),
        "pending": _safe(_pending, cfg, now, default=[]),
        "handoff": _safe(_read_handoff, cfg, fresh, wake_kind),
        "handoff_title": ncfg.get("handoff_title", "阿屿の碎碎念"),
        "replay": _safe(
            _replay_events, conn, cfg,
            bcfg.get("replay_events", 6),
            bcfg.get("replay_event_chars", 300),
            default=[],
        ),
        "replay_title": ncfg.get("replay_title", "最近对话回放"),
    }


def _build_budget(conn, cfg, now, kv, bcfg) -> dict:
    five = kv.get(_FIVE_HOUR[0])
    seven = kv.get(_SEVEN_DAY[0])
    reset = kv.get(_FIVE_HOUR[1])
    return {
        "five_h_pct": _as_float(five),
        "five_h_reset": _local_hm(reset, cfg) if reset else None,
        "seven_d_pct": _as_float(seven),
        "window_tokens": _window_tokens(conn),
        "today_tokens": _today_tokens(conn, now),
        "daily_budget": int(bcfg.get("daily_budget", 1_000_000)),
    }


def _as_float(raw):
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def render(cfg: dict, now: datetime, data: dict) -> str:
    """Pure assembly: data dict -> wakeup note text. No DB / no I/O."""
    lines: list[str] = []

    lines.append("Wake: " + " | ".join(data.get("wake_parts") or ["巡回"]))

    now_seg = f"Now: {now.strftime('%H:%M %a')}"
    last = data.get("last_wake")
    if last:
        seg = f"Last wake: {last['minutes_ago']}min ago"
        if last.get("force_slept"):
            seg += " (force-slept mid-task)"
        now_seg += f" | {seg}"
    lines.append(now_seg)

    budget_line = _render_budget(data.get("budget"))
    if budget_line:
        lines.append(budget_line)

    app = data.get("active_app")
    if app:
        lines.append(f"Active (Mac): {app}")

    cal = data.get("cal")
    if cal:
        segs = []
        if cal.get("current"):
            segs.append(f"Current {cal['current']}")
        if cal.get("next"):
            segs.append(f"Next {cal['next']}")
        if segs:
            lines.append("Cal: " + " | ".join(segs))

    rem = data.get("rem_last_done")
    if rem:
        lines.append(f"Rem: {rem}")

    pending = data.get("pending") or []
    if pending:
        segs = [f"due {p['hm']} {p['intent']}".rstrip() for p in pending]
        lines.append("Pending self-schedule: " + " · ".join(segs))

    handoff = data.get("handoff")
    if handoff:
        lines.append(f"{data.get('handoff_title', '阿屿の碎碎念')}: {handoff}")

    replay = data.get("replay") or []
    if replay:
        lines.append(f"{data.get('replay_title', '最近对话回放')}:")
        for ev in replay:
            lines.append(f"  [{ev['channel']} {ev['hm']}] {ev['content']}")

    return "\n".join(lines)


def _render_budget(budget: dict | None) -> str | None:
    if not budget:
        return None
    parts = []
    # five_h_pct / seven_d_pct are UTILIZATION (fraction USED); the quota-health
    # signal is what's LEFT, so display remaining = 100 - used (0% used -> 100%
    # left). Clamped to [0,100] so a slight overshoot never prints a negative.
    five = budget.get("five_h_pct")
    if five is not None:
        seg = f"5h {_remaining(five):.0f}% left"
        if budget.get("five_h_reset"):
            seg += f" (reset {budget['five_h_reset']})"
        parts.append(seg)
    seven = budget.get("seven_d_pct")
    if seven is not None:
        parts.append(f"7d {_remaining(seven):.0f}% left")
    window = budget.get("window_tokens")
    if window is not None:
        parts.append(f"net {window // 1000}k")
    daily = int(budget.get("daily_budget", 1_000_000))
    today = int(budget.get("today_tokens", 0))
    pct = (today / daily * 100) if daily else 0
    parts.append(f"today {today // 1000}k/{_fmt_budget(daily)} {pct:.0f}%")
    return "Budget: " + " · ".join(parts) if parts else None


def _remaining(used_pct: float) -> float:
    """Utilization (used) -> remaining, clamped to [0, 100]."""
    return max(0.0, min(100.0, 100.0 - used_pct))


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
    _data = gather(_conn, _cfg, _now, fresh=True, wake_kind="rebirth")
    print(render(_cfg, _now, _data))
    _conn.close()
