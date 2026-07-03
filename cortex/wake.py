"""Wake runner (C3): on a pacemaker wake decision, assemble the bulletin,
call marrow's resumed full-env cortex session, persist the session_id, and
refresh day_log.md. Daily rebirth: first wake on a new local date starts a
fresh marrow session (no resume_sid) and archives the previous day_log.

marrow lives in its own repo/venv (separate deps) — invoked as a subprocess
against marrow's own venv python rather than imported in-process, so cortex
stays decoupled (Frame: "own project, sibling of marrow").
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from cortex import bulletin, config, day_log, db, symlinks
from cortex.pacemaker import integration
from cortex.pacemaker.core import PacemakerState

_PATH_ENV = (
    f"{os.path.expanduser('~/.local/bin')}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
)

_MARROW_CALL_SCRIPT = (
    "import sys, json\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from marrow.llm import LLMClient\n"
    "prompt = sys.stdin.read()\n"
    "client = LLMClient()\n"
    "result = client.call_cortex(prompt, cwd=sys.argv[2], resume_sid=(sys.argv[3] or None))\n"
    "print(json.dumps(result))\n"
)


class WakeError(Exception):
    pass


def _now(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def assemble_bulletin(conn: sqlite3.Connection, cfg: dict, now: datetime, decision: dict | None = None) -> str:
    """Thin wrapper: gather() + render(), cal_next_3h not wired yet (schedule.py
    ownership transfers at C6) — bulletin honestly renders 'Calendar (3h): none'."""
    data = bulletin.gather(conn, cfg, now, decision=decision)
    return bulletin.render(cfg, now, data)


def call_marrow_cortex(prompt: str, cwd: str, resume_sid: str | None, cfg: dict) -> dict:
    """Spawn marrow's own venv python to run LLMClient.call_cortex. Returns
    {"text": str, "session_id": str | None}. Raises WakeError on failure."""
    mcfg = cfg["marrow"]
    python = os.path.expanduser(mcfg["venv_python"])
    repo_dir = os.path.expanduser(mcfg["repo_dir"])
    timeout = mcfg.get("call_timeout_s", 320)
    env = {**os.environ, "PATH": _PATH_ENV + ":" + os.environ.get("PATH", "")}
    try:
        proc = subprocess.run(
            [python, "-c", _MARROW_CALL_SCRIPT, repo_dir, cwd, resume_sid or ""],
            input=prompt, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise WakeError(f"marrow call_cortex timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise WakeError(f"marrow call_cortex failed: {proc.stderr.strip()[-2000:]}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise WakeError(f"marrow call_cortex returned unparseable output: {proc.stdout[:500]}") from e


def run_wake(
    conn: sqlite3.Connection,
    cfg: dict,
    decision: dict,
    now: datetime | None = None,
    caller=call_marrow_cortex,
) -> dict:
    """Full wake pipeline against real data. `caller` is injectable so tests
    never spawn a real claude process. Returns the caller's result dict."""
    now = now or _now(cfg)
    today = now.date().isoformat()

    symlinks.ensure_all(cfg)

    state = integration.load_state(conn)
    rebirth = state.cortex_session_date != today
    resume_sid = None if rebirth else state.cortex_session_id

    path = config.day_log_path(cfg)
    if rebirth and path.exists():
        day_log.archive(path, config.day_log_archive_dir(cfg))
    if rebirth or not path.exists():
        day_log.new_day(path, today)

    bulletin_text = assemble_bulletin(conn, cfg, now, decision=decision)
    home = str(config.cortex_home(cfg))

    result = caller(bulletin_text, home, resume_sid, cfg)

    new_state = PacemakerState(
        desire=state.desire,
        expect_reply=state.expect_reply,
        next_floor_due_at=state.next_floor_due_at,
        last_wake_at=state.last_wake_at,
        cortex_session_id=result.get("session_id") or resume_sid,
        cortex_session_date=today,
    )
    integration.save_state(conn, new_state)

    day_log.update(path, conn, cfg, now)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual cortex wake entry point (supervised)")
    parser.add_argument("--force", action="store_true", help="bypass pacemaker gates, wake now")
    parser.add_argument("--print-bulletin", action="store_true",
                         help="assemble + print the real bulletin only, no marrow call")
    args = parser.parse_args(argv)

    cfg = config.load()
    conn = db.connect(cfg)
    try:
        now = _now(cfg)
        if args.print_bulletin:
            text = assemble_bulletin(conn, cfg, now)
            print(text)
            print(f"\n[{len(text)} chars]", file=sys.stderr)
            return 0
        if args.force:
            decision = {"wake": True, "reasons": [], "gated_by": [],
                        "explanation": f"{now.strftime('%H:%M')} manual --force wake"}
            run_wake(conn, cfg, decision, now=now)
            return 0
        parser.print_help()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
