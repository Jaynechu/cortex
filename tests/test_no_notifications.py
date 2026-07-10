"""Repo guard: macOS notifications are banned in cortex. The ONLY permitted
attention-getter is bringing the iTerm window to the front (say() = sound +
front). An osascript `display notification` has crept back before; enforcement
lives here (CI), not in docs — a source scan fails if any cortex/ file contains
`display notification`."""
from __future__ import annotations

import pathlib

_BANNED = "display notification"
_PKG_ROOT = pathlib.Path(__file__).resolve().parent.parent / "cortex"


def test_no_display_notification_in_source():
    offenders = []
    for path in _PKG_ROOT.rglob("*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _BANNED in text:
            offenders.append(str(path))
    assert not offenders, (
        f"macOS notifications are banned in cortex — {_BANNED!r} found in: "
        f"{offenders}. Use say() (bring iTerm to front) instead.")
