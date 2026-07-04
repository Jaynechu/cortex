"""Wake-latency probe: append monotonic-delta stage marks for one wake to a
shared timing log so the next real wake reveals the slow stage. Always-on,
best-effort — never raises into the wake path. The marrow subprocess appends
its own stream-event marks to the same file under the same wake id."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class WakeTimer:
    def __init__(self, path, wake_id: str, origin: float | None = None):
        self.path = Path(os.path.expanduser(str(path)))
        self.wake_id = wake_id
        self.origin = origin if origin is not None else time.monotonic()
        self.last = self.origin

    def mark(self, stage: str, at: float | None = None) -> None:
        try:
            t = at if at is not None else time.monotonic()
            total = (t - self.origin) * 1000.0
            delta = (t - self.last) * 1000.0
            self.last = t
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(f"{_utcnow_iso()} wake={self.wake_id} {stage} "
                        f"t=+{total:.0f}ms d=+{delta:.0f}ms\n")
        except Exception:
            pass
