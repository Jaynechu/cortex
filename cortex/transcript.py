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
