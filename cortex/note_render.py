"""Render-only CLI: print a FRESH wakeup note to stdout, no side effects.

The wake-time note is assembled once and frozen to disk; a rotated window then
gets a stale file. This entry re-renders at injection time so "Now:" and the
Window SID always reflect the caller's current moment and transcript.

Contract: read-only. No ct_wake_log writes, no wake_state writes, no file
writes. --transcript supplies the Window-line SID (Path(...).stem[:8]) — the
caller's own transcript, correct even after rotation. Print the note; exit 0.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cortex import config, db, note


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a fresh wakeup note.")
    parser.add_argument("--transcript", default=None,
                        help="caller transcript path; stem[:8] -> Window SID")
    parser.add_argument("--no-ct", action="store_true",
                        help="skip ct-note peek — the marrow hook delivers ct "
                             "notes via outbox.deliver, so rendering them here "
                             "would double them in the same payload")
    args = parser.parse_args()

    cfg = config.load()
    tz = ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))
    now = datetime.now(tz)

    window_sid = None
    if args.transcript:
        window_sid = Path(str(args.transcript)).stem[:8]

    conn = db.connect(cfg)
    try:
        data = note.gather(conn, cfg, now, window_sid=window_sid)
        if args.no_ct:
            data["ct_notes"] = []
        print(note.render(cfg, now, data))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
