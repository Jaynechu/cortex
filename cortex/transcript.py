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


def mtime(cfg: dict) -> float | None:
    p = newest(cfg)
    return p.stat().st_mtime if p else None


def window_tokens(cfg: dict) -> int:
    """Context-window occupancy = the last assistant message's usage totals
    (input + cache read + cache creation + output). Grows with the conversation;
    drives rotate (/clear) + fuse thresholds. 0 if no transcript/usage yet."""
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
