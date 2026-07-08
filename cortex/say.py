"""cortex.say — the 开口 primitive as a CLI, so the cortex session (running
inside its own claude window) can request the user's attention: a quiet macOS
notification, no focus steal. Optional --note overrides the notification body.

Usage: python -m cortex.say [--note "text"]
"""
from __future__ import annotations

import argparse
import sys

from cortex import config, window


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Quietly request the user's attention")
    parser.add_argument("--note", default=None, help="override the notification body")
    args = parser.parse_args(argv)
    cfg = config.load()
    window.say(cfg, note=args.note)
    return 0


if __name__ == "__main__":
    sys.exit(main())
