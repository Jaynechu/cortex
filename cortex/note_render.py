"""Render-only CLI: print a FRESH wakeup note to stdout, no side effects.

The wake-time note is assembled once and frozen to disk; a rotated window then
gets a stale file. This entry re-renders at injection time so "Now:" and the
Window SID always reflect the caller's current moment and transcript.

Contract: no ct_wake_log writes, no wake_state writes, no shell ledger writes.
--transcript supplies the Window-line SID (Path(...).stem[:8]) — the caller's
own transcript, correct even after rotation. Print the note; exit 0.

--replay is the one exception to read-only: a headless consumer has no
UserPromptSubmit hook, so the note is its only replay channel and this render
consumes that shell's private replay marker.
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
    parser.add_argument("--shell", default=None,
                        help="shell id this note is rendered for; its own "
                             "channel drops out of Replay so the note does not "
                             "replay the shell back at itself "
                             "([note].shell_replay_exclude) and it uses its own "
                             "replay marker. Unset = the unqualified (cli) set")
    parser.add_argument("--replay", action="store_true",
                        help="include the Replay section — headless consumers "
                             "only (no UserPromptSubmit hook, so the note is "
                             "their only replay outlet). Consumes this shell's "
                             "replay marker; a window render must never pass it")
    args = parser.parse_args()

    cfg = config.load()
    if args.shell:
        cfg = note.for_shell(cfg, args.shell)
    tz = ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))
    now = datetime.now(tz)

    window_sid = None
    if args.transcript:
        window_sid = Path(str(args.transcript)).stem[:8]

    conn = db.connect(cfg)
    try:
        data = note.gather(conn, cfg, now, window_sid=window_sid,
                           shell=args.shell, replay=args.replay)
        if args.no_ct:
            data["ct_notes"] = []
        print(note.render(cfg, now, data))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
