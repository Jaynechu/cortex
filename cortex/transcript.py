"""Read the resident cortex interactive session transcript. Claude Code writes
one JSONL per session under ~/.claude/projects/<munged-cwd>/; newest file =
current window. Exposes mtime + window-token occupancy (last assistant usage).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from cortex import config


def _munge(path: str) -> str:
    """Claude Code munges the cwd into the projects dir name by replacing every
    non-alphanumeric char with '-' (verified: /Users/.../.config/marrow/cortex
    -> -Users-...--config-marrow-cortex)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def transcript_dir(cfg: dict) -> Path:
    raw = cfg["paths"].get("transcript_dir") or ""
    if raw:
        return Path(raw).expanduser()
    home = str(config.cortex_home(cfg))
    return Path.home() / ".claude" / "projects" / _munge(home)


def newest(cfg: dict) -> Path | None:
    d = transcript_dir(cfg)
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


_LINEAGE_SCAN_LINES = 20  # first user message shows up within the first few
                          # lines (queue-operation/mode/permission-mode/
                          # file-history-snapshot preamble); no need to scan far.


def _first_user_content(p: Path) -> str | None:
    """First user-role message's content (str form) within the first
    _LINEAGE_SCAN_LINES lines of a session jsonl. Minimal parse — stops at the
    first match, never loads the whole file. None if no user message found in
    that window or the file is unreadable."""
    try:
        with p.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= _LINEAGE_SCAN_LINES:
                    break
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                msg = o.get("message")
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        # Content-block form: concatenate text blocks only.
                        parts = [b.get("text", "") for b in content
                                if isinstance(b, dict) and b.get("type") == "text"]
                        return "".join(parts)
                    return None
    except OSError:
        return None
    return None


# The baked first prompt is "<emoji> <marker> HH:MM" (see window.fresh_initial_
#_prompt) — a short line. The marker sits a few chars in (emoji + space), so a
# bounded PREFIX search (not a full-content substring scan) is what actually
# discriminates a window-lineage session from a headless one: a genuine
# window's first message is short and the marker sits near its start; a digest
# archive's first message is a multi-KB blob that can quote/contain the marker
# substring deep inside it too (live-confirmed) but never near its start.
_LINEAGE_MARKER_PREFIX_CHARS = 24


def newest_window_lineage(cfg: dict, marker: str) -> Path | None:
    """The newest session jsonl in the transcript dir whose FIRST user message
    carries the wake signal marker (config wake.wake_signal_marker, e.g.
    '[CORTEX-WAKE]') within its first _LINEAGE_MARKER_PREFIX_CHARS chars — i.e.
    a genuine window-lineage session (every cortex window since dccb3d4 is
    launched with the marker baked into its first prompt), not a headless
    one-shot (marrow's sessionend digest also runs `claude -p` against this
    same cwd, so its archive lands in the same projects dir and can be the
    mtime-newest file, yet its first message is a large archived/quoted blob
    that can contain the marker substring far from its start).

    Iterates candidates by mtime DESC and returns the first hit; digest/
    headless archives fail the check and are skipped. None if no candidate
    qualifies (caller falls back to the recorded hint)."""
    d = transcript_dir(cfg)
    if not d.exists():
        return None
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        content = _first_user_content(p)
        if content is not None and marker in content[:_LINEAGE_MARKER_PREFIX_CHARS]:
            return p
    return None


def mtime(cfg: dict) -> float | None:
    p = newest(cfg)
    return p.stat().st_mtime if p else None


# Bytes read from the tail on each silence poll — a handful of turns is plenty to
# find the last real user message; capped so a multi-MB transcript never loads
# whole (benchmark: 8 KiB tail read is sub-millisecond regardless of file size).
_TAIL_BYTES = 65536


# Leading decoration tolerated before a machine marker: whitespace + at most a
# couple of decoration glyphs (⏳U+23F3 ☀️U+2600 ⚙️U+2699), VS16, ZWJ. Narrowed
# to real decoration ranges — deliberately EXCLUDES CJK/kana/hangul so a Chinese
# user message quoting a marker keeps its lead char and still resets the timer.
_MARKER_LEAD_RE = re.compile(
    r"^\s*(?:[\U00002300-\U000027BF\U00002B00-\U00002BFF"
    r"\U0001F300-\U0001FAFF\U0000FE0F\U0000200D]\s*){0,3}")


def _line_starts_with_marker(text: str, markers: list[str]) -> bool:
    """True iff ANY line of *text* begins with a machine marker after a tolerated
    leading decoration run. Machine writes (wake bell '<emoji> [CORTEX-WAKE] …',
    tuck-in block whose final line is '⏳ [NEW ROUND] …') always line-start their
    marker; a real user message merely quoting one mid-sentence never matches, so
    it still resets the silence timer (zero false positives on real speech)."""
    if not markers:
        return False
    for line in text.splitlines() or [text]:
        head = _MARKER_LEAD_RE.sub("", line, count=1)
        if any(head.startswith(mk) for mk in markers):
            return True
    return False


def _line_markers(cfg: dict) -> list[str]:
    """Machine-line markers that identify a NON-user turn arriving down the ear
    channel (wake bell, tuck-in / free-round injection). A transcript entry whose
    user-role text contains any of these is a system write, NOT a real user
    message, so it must not reset the silence timer. Aligned with marrow's
    is_machine_line (cortex_bridge.py): wake marker + tuck-in marker family."""
    wcfg = cfg.get("wake", {})
    out = []
    m = str(wcfg.get("wake_signal_marker") or "").strip()
    if m:
        out.append(m)
    for m in wcfg.get("machine_line_markers") or config.DEFAULT_MACHINE_LINE_MARKERS:
        m = str(m).strip()
        if m and m not in out:
            out.append(m)
    return out


def _user_text(msg: dict) -> str | None:
    """String form of a user-role message's content, or None when it is not a
    user turn / has no text (tool_result blocks yield "")."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)
    return ""


def _entry_ts(o: dict) -> float | None:
    """Epoch seconds of a transcript entry's ISO `timestamp`, or None."""
    from datetime import datetime
    raw = o.get("timestamp")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp()


def last_user_message_mtime(cfg: dict) -> float | None:
    """Epoch-seconds timestamp of the LAST real user message in the current
    transcript, tail-read (never loads the whole file). Assistant turns, system
    writes and the ear-delivered injections (wake bell / tuck-in / free-round /
    night lines — `_line_markers`) do NOT count: those are machine writes that
    must not reset the silence timer (the "永远睡不到alarm" bug family). Returns
    None when no qualifying user message is found in the tail or the transcript is
    missing/unreadable — callers fall back to hold behaviour."""
    p = newest(cfg)
    if not p:
        return None
    markers = _line_markers(cfg)
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > _TAIL_BYTES:
                f.seek(size - _TAIL_BYTES)
                f.readline()  # drop the partial first line
            chunk = f.read()
    except OSError:
        return None
    latest: float | None = None
    for raw in chunk.splitlines():
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        text = _user_text(o.get("message"))
        if text is None:
            continue  # not a user turn
        if _line_starts_with_marker(text, markers):
            continue  # machine line down the ear channel, not a real user turn
        ts = _entry_ts(o)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def user_silent_min(cfg: dict) -> float | None:
    """Minutes since the last real user message (`last_user_message_mtime`), or
    None when it cannot be determined. The single silence source shared by the
    watchdog poll and the tick awake gate."""
    import time
    ts = last_user_message_mtime(cfg)
    return (time.time() - ts) / 60.0 if ts is not None else None


def window_tokens(cfg: dict) -> int:
    """Context-window occupancy = the last assistant message's usage totals
    (input + cache read + cache creation + output). Grows with the conversation;
    drives rotate (respawn) + fuse thresholds. 0 if no transcript/usage yet."""
    p = newest(cfg)
    if not p:
        return 0
    total = 0
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        msg = o.get("message")
        u = msg.get("usage") if isinstance(msg, dict) else None
        if u:
            total = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                     + u.get("cache_creation_input_tokens", 0) + u.get("output_tokens", 0))
    return total
