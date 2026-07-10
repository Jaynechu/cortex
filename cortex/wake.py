"""Wake runner (C3): on a pacemaker wake decision, assemble the wakeup note,
call marrow's resumed full-env cortex session, and persist the session_id.
Freshness (a fresh marrow session, no resume_sid) comes only from the
rotate/dead-window detection: a rotated or dead resident window is a new brain
that reads the previous brain's handoff via SessionStart. Night close (23:00)
retires the resident session so the first post-night wake is a plain fresh spawn.

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
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cortex import config, db, note, symlinks
from cortex.pacemaker import integration
from cortex.timing import WakeTimer

# Seconds added to the inner marrow claude-call budget (marrow.call_timeout_s)
# to derive the outer subprocess kill deadline. The inner threading.Timer must
# fire first (clean LLMError) before this outer subprocess.run timeout does;
# the margin covers nested-python startup + marrow import.
_OUTER_TIMEOUT_MARGIN_S = 30

_PATH_ENV = (
    f"{os.path.expanduser('~/.local/bin')}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
)

_MARROW_CALL_SCRIPT = (
    "import sys, json\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from marrow.llm import LLMClient\n"
    "prompt = sys.stdin.read()\n"
    "client = LLMClient()\n"
    "result = client.call_cortex(prompt, cwd=sys.argv[2], "
    "resume_sid=(sys.argv[3] or None), timeout=int(sys.argv[4]), "
    "max_tokens=int(sys.argv[5]))\n"
    "print(json.dumps(result))\n"
)


class WakeError(Exception):
    pass


def _now(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def assemble_note(conn: sqlite3.Connection, cfg: dict, now: datetime,
                  decision: dict | None = None, fresh: bool = False,
                  wake_kind: str | None = None) -> str:
    """Thin wrapper: gather() + render(). `fresh`/`wake_kind` gate the handoff
    (碎碎念) section — only a fresh window (rotate) receives it."""
    data = note.gather(conn, cfg, now, decision=decision,
                       fresh=fresh, wake_kind=wake_kind)
    return note.render(cfg, now, data)


def call_marrow_cortex(prompt: str, cwd: str, resume_sid: str | None, cfg: dict) -> dict:
    """Spawn marrow's own venv python to run LLMClient.call_cortex. Returns
    {"text": str, "session_id": str | None}. Raises WakeError on failure."""
    mcfg = cfg["marrow"]
    python = os.path.expanduser(mcfg["venv_python"])
    repo_dir = os.path.expanduser(mcfg["repo_dir"])
    # Single source of truth: call_timeout_s is the inner claude-call budget,
    # passed down so marrow enforces exactly this value; the outer subprocess
    # kill is derived (inner + margin) so it never fires before the inner one.
    inner_timeout = int(mcfg.get("call_timeout_s", 600))
    outer_timeout = inner_timeout + _OUTER_TIMEOUT_MARGIN_S
    token_cap = int(cfg.get("wake", {}).get("token_cap", 150_000))
    # CORTEX_WAKE_ID / CORTEX_WAKE_TIMING_LOG (set by run_wake) ride os.environ
    # into the marrow subprocess so its stream-event marks share this wake.
    env = {**os.environ, "PATH": _PATH_ENV + ":" + os.environ.get("PATH", "")}
    try:
        proc = subprocess.run(
            [python, "-c", _MARROW_CALL_SCRIPT, repo_dir, cwd,
             resume_sid or "", str(inner_timeout), str(token_cap)],
            input=prompt, capture_output=True, text=True, timeout=outer_timeout, env=env,
        )
    except subprocess.TimeoutExpired as e:
        raise WakeError(f"marrow call_cortex timed out after {outer_timeout}s") from e
    if proc.returncode != 0:
        raise WakeError(f"marrow call_cortex failed: {proc.stderr.strip()[-2000:]}")
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise WakeError(f"marrow call_cortex returned unparseable output: {proc.stdout[:500]}") from e


def _audit_wake(conn: sqlite3.Connection, wake_id: str, summary: str) -> None:
    """Best-effort one-line audit of a wake outcome (token-cap breach /
    failure). audit_log lives on the shared marrow DB; swallow if absent."""
    try:
        conn.execute(
            "INSERT INTO audit_log (target_table, action, summary) VALUES (?, ?, ?)",
            ("ct_wake_log", "cortex_wake", f"wake={wake_id} {summary}"),
        )
        conn.commit()
    except Exception:
        pass


def _force_fresh_next(conn: sqlite3.Connection, state, today: str) -> None:
    """Next wake starts a fresh marrow session (drop resume sid).
    Used on token-cap breach and marrow call failure/timeout so a broken/oversized
    session is never resumed."""
    integration.save_state(conn, replace(state, cortex_session_id=None))


_DAYBRIEF_TIMEOUT_S = 20


def _render_daybrief(cfg: dict) -> None:
    """Re-render marrow's daybrief.md after a wake. marrow owns the renderer
    (own venv/deps) — invoked as a subprocess against marrow's venv python,
    same pattern as call_marrow_cortex. Best-effort: never raises, never
    blocks the wake return."""
    python = os.path.expanduser(cfg["marrow"]["venv_python"])
    try:
        subprocess.run(
            [python, "-m", "marrow.daybrief"],
            capture_output=True, text=True, timeout=_DAYBRIEF_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - must not kill the wake
        pass


def _latest_wake_log_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM ct_wake_log WHERE wake = 1 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def _schedule_reasons(decision: dict) -> list[dict]:
    """Fired schedule (duty) reasons as fact dicts (name/prompt_path)."""
    out = []
    for r in decision.get("reasons", []) or []:
        kind = r.get("kind") if isinstance(r, dict) else getattr(r, "kind", "")
        if kind == "schedule":
            facts = r.get("facts", {}) if isinstance(r, dict) else getattr(r, "facts", {})
            out.append(dict(facts or {}))
    return out


def _duty_prompt(duty: dict) -> str | None:
    """Read the duty's prompt_path (the actual task instructions for a
    schedule/duty wake). Missing/unreadable file -> None, never crashes."""
    raw = duty.get("prompt_path")
    if not raw:
        return None
    path = Path(os.path.expanduser(str(raw)))
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _schedule_wake(conn, cfg, decision, now, duties) -> dict:
    """Schedule (duty) wake: a fresh iTerm window per duty (attention hygiene —
    no roaming context, no 碎碎念). Not the resident session and not resumed;
    cortex ends it itself when the duty is done. A quiet say() requests
    attention on spawn. Budget/night-exempt (schedule pierces the gates).

    The wakeup note (Wake/budget line) gives minimal orientation; the duty's
    prompt_path (if set and readable) carries the actual task instructions and
    is appended after it — schedule wakes are pure-work windows, so the duty
    prompt is the main payload. Missing/unreadable prompt file -> generic note
    only, logged, never crashes."""
    from cortex import window

    home = str(config.cortex_home(cfg))
    for duty in duties:
        name = duty.get("name") or "duty"
        note_text = assemble_note(conn, cfg, now, decision=decision,
                                  fresh=False, wake_kind="schedule")
        duty_prompt = _duty_prompt(duty)
        if duty.get("prompt_path") and duty_prompt is None:
            _audit_wake(conn, wake_id_of(now),
                        f"schedule duty prompt_path unreadable: {name}")
        if duty_prompt:
            note_text = f"{note_text}\n\n{duty_prompt}"
        try:
            sid = window.spawn_fresh(cfg)
            window.inject_note(cfg, note_text, sid=sid)
            window.say(cfg)  # quiet attention request (notification), no focus steal
        except window.WindowError:
            _audit_wake(conn, wake_id_of(now), f"schedule window failed: {name}")
            continue
        integration.mark_schedule_fired(conn, name, now.date().isoformat())
    return {"mode": "schedule", "session_id": None, "text": None, "duties": duties}


def wake_id_of(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


def _window_rotated(cfg) -> bool:
    """Structural freshness check for the resident window: has its brain been
    replaced since the last wake? A /clear (self- or proxy-typed) starts a NEW
    interactive session -> a new transcript jsonl (verified empirically), so the
    newest transcript differs from the one recorded at set_awake; a respawned or
    first-ever window has no recorded hint. The rotate flag (set by lie_down when
    it types /clear) is the belt-and-braces path for the crash/respawn case where
    the transcript may not have rolled yet. A session that exists but whose
    `claude` process died (SIGINT/crash/manual ctrl-C -> bare shell) is caught
    here too: ensure_window relaunches it AFTER this check runs, so the
    transcript diff alone would still see the stale (dead) transcript at this
    point -- checked directly instead of relying on that diff. Either signal ->
    rotated."""
    from cortex import transcript, wake_state, window

    if wake_state.take_rotated(cfg):
        return True
    sid = wake_state.get_session_id(cfg)
    if not sid or not window.is_running() or not window._session_alive(sid):
        return True  # window died / never existed -> respawn is a fresh brain
    if window.find_claude_pid(cfg) is None:
        return True  # session alive but claude dead -> ensure_window relaunches
    prev = wake_state.load(cfg).get("transcript")
    cur = transcript.newest(cfg)
    cur = str(cur) if cur else None
    # A None recorded hint means the last spawn timed out before the new
    # session jsonl appeared (see _spawn_wake). The window is alive with no
    # rotate flag, so treat it as NOT rotated — otherwise cur (any current
    # transcript) != None would re-trigger a respawn every tick (the loop this
    # whole fix removes). Only a real transcript-to-transcript mismatch rotates.
    if prev is None:
        return False
    return cur != prev


def _signal_landed(cfg, before: float | None, timeout_sec: float) -> bool:
    """After appending a wake signal, poll the transcript mtime for up to
    timeout_sec: a growing transcript = the ear fired and cortex is awake.
    `before` is the mtime captured just before the append (None = no transcript
    yet, any new activity counts)."""
    from cortex import transcript

    step = 3.0
    waited = 0.0
    while waited < timeout_sec:
        time.sleep(min(step, timeout_sec - waited))
        waited += step
        after = transcript.mtime(cfg)
        if after is not None and (before is None or after > before):
            return True
    return False


# Bounded poll for a freshly-spawned window's NEW session transcript to appear.
# The launched claude does not create its session jsonl until it starts its
# first turn, so newest() right after respawn returns the PREVIOUS session's
# file — recording that stale hint makes _window_rotated see a mismatch on the
# next tick and respawn forever (the P0 loop). Poll until a file newer than the
# pre-spawn newest (or past the pre-spawn timestamp) shows up; on timeout record
# None so _window_rotated's None-hint guard keeps the alive window unrotated.
_SPAWN_TRANSCRIPT_POLL_STEP_S = 0.5
_SPAWN_TRANSCRIPT_POLL_TIMEOUT_S = 8.0


def _wait_new_transcript(cfg, prev_path: str | None, spawn_ts: float) -> str | None:
    """Poll (bounded) for the new session transcript after a spawn. Returns its
    path once one appears that differs from prev_path and was modified at/after
    spawn_ts; None on timeout (record None, never a stale path)."""
    from cortex import transcript

    waited = 0.0
    while waited < _SPAWN_TRANSCRIPT_POLL_TIMEOUT_S:
        cur = transcript.newest(cfg)
        if cur is not None:
            cur_s = str(cur)
            try:
                fresh_mtime = cur.stat().st_mtime >= spawn_ts
            except OSError:
                fresh_mtime = False
            if cur_s != prev_path or fresh_mtime:
                return cur_s
        time.sleep(_SPAWN_TRANSCRIPT_POLL_STEP_S)
        waited += _SPAWN_TRANSCRIPT_POLL_STEP_S
    return None


def _spawn_wake(conn, cfg, note_path, now) -> dict | None:
    """Fresh-window wake (respawn / rotate / dead window / ear-miss recovery):
    spawn a new resident window whose FIRST prompt is the wakeup-note Read line,
    baked into the launch command — the window starts acting immediately, with
    no arm prompt, no lie-down-first, and no signal append. Sets the awake
    marker + lights the watchdog. Returns a result dict, or None on window
    failure (caller -> headless).

    The recorded transcript hint must be the NEW session's jsonl — captured only
    after it actually appears (bounded poll) — never the pre-spawn newest, which
    is the OLD session and would drive an endless respawn loop next tick."""
    from cortex import transcript, wake_state, watchdog, window

    prev_path = transcript.newest(cfg)
    prev_path = str(prev_path) if prev_path else None
    spawn_ts = time.time()
    try:
        window.respawn(cfg, initial_prompt=window.note_read_line(cfg, note_path))
    except window.WindowError:
        return None
    new_path = _wait_new_transcript(cfg, prev_path, spawn_ts)
    wake_state.set_awake(cfg, _latest_wake_log_id(conn), new_path)
    watchdog.spawn(cfg)
    return {"mode": "window", "session_id": None, "text": None}


def _window_wake(conn, cfg, note_text, now, respawn: bool = False) -> dict | None:
    """Interactive wake. A fresh brain (rotate/dead/rotated window) is spawned
    with the note as its first prompt (_spawn_wake) — no ear, no ritual. An
    alive resident window is woken via the signal-file ear: write the note file,
    append a WAKE line its armed Monitor tails, then verify the wake landed
    (transcript mtime grows within ear_timeout_sec). On an ear miss the resident
    is replaced by a fresh window that gets the note directly. Sets the awake
    marker + lights the watchdog. Returns a result dict, or None if the window
    path failed (caller -> headless). The wake is NOT over here — lie_down (self
    or watchdog proxy) ends it."""
    from cortex import transcript, wake_state, watchdog, window

    note_path = str(window.write_note(cfg, note_text))

    # Fresh brain: rotate/rebirth or a dead resident -> spawn with the note baked
    # in as the first prompt (no ear, no arm/lie-down dance).
    if respawn or not _window_alive(cfg):
        return _spawn_wake(conn, cfg, note_path, now)

    # Alive resident: the signal-file ear path (unchanged).
    timeout = float(cfg["wake"].get("ear_timeout_sec", 90))
    try:
        before = transcript.mtime(cfg)
        window.append_wake_signal(cfg, note_path)
        if not _signal_landed(cfg, before, timeout):
            _audit_wake(conn, wake_id_of(now), "ear miss -> respawn with note")
            return _spawn_wake(conn, cfg, note_path, now)
    except window.WindowError:
        return None
    tpath = transcript.newest(cfg)
    wake_state.set_awake(cfg, _latest_wake_log_id(conn),
                         str(tpath) if tpath else None)
    watchdog.spawn(cfg)
    return {"mode": "window", "session_id": None, "text": None}


def _window_alive(cfg) -> bool:
    """The resident window exists, iTerm is up, and its `claude` is running."""
    from cortex import wake_state, window

    sid = wake_state.get_session_id(cfg)
    if not sid or not window.is_running() or not window._session_alive(sid):
        return False
    return window.find_claude_pid(cfg) is not None


def run_wake(
    conn: sqlite3.Connection,
    cfg: dict,
    decision: dict,
    now: datetime | None = None,
    caller=call_marrow_cortex,
    tick_started: float | None = None,
    gate_done: float | None = None,
) -> dict:
    """Full wake pipeline against real data. `caller` is injectable so tests
    never spawn a real claude process. Returns the caller's result dict.
    `tick_started`/`gate_done` are monotonic anchors from pacemaker_tick so the
    latency probe covers tick fire -> gate eval -> the wake chain."""
    now = now or _now(cfg)
    today = now.date().isoformat()

    wake_id = f"{now.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"
    timing_path = config.wake_timing_log_path(cfg)
    origin = tick_started if tick_started is not None else time.monotonic()
    timer = WakeTimer(timing_path, wake_id, origin=origin)
    if tick_started is not None:
        timer.mark("tick_fire", at=tick_started)
    if gate_done is not None:
        timer.mark("gate_eval", at=gate_done)
    os.environ["CORTEX_WAKE_ID"] = wake_id
    os.environ["CORTEX_WAKE_TIMING_LOG"] = str(timing_path)

    symlinks.ensure_all(cfg)
    timer.mark("symlinks")

    # Schedule (duty) wakes short-circuit here: a fresh window per duty, never
    # the resident session, never resumed, no daybrief render — pure干活.
    duties = _schedule_reasons(decision)
    if duties and cfg["wake"].get("mode", "window") == "window" and caller is call_marrow_cortex:
        result = _schedule_wake(conn, cfg, decision, now, duties)
        timer.mark("schedule_spawned")
        timer.mark("wake_complete")
        return result

    state = integration.load_state(conn)
    resume_sid = state.cortex_session_id
    timer.mark("resume")

    note_text = assemble_note(conn, cfg, now, decision=decision)
    home = str(config.cortex_home(cfg))
    timer.mark("note")

    # Interactive path (B3v): the resident iTerm window is the cortex body. Only
    # taken for the real wake (default caller) in window mode; explicit `caller`
    # (tests / headless callers) always runs the marrow-subprocess path below.
    if cfg["wake"].get("mode", "window") == "window" and caller is call_marrow_cortex:
        # A rotated/dead window is a fresh brain that must read the old brain's
        # handoff note -> respawn (SIGTERM claude + fresh window that gets the
        # note as its first prompt), so the same path serves rotate and a dead
        # window.
        window_text = note_text
        respawn = _window_rotated(cfg)
        if respawn:
            window_text = assemble_note(
                conn, cfg, now, decision=decision, fresh=True, wake_kind="rotate")
            timer.mark("rotate_note")
        win = _window_wake(conn, cfg, window_text, now, respawn=respawn)
        if win is not None:
            timer.mark("window_injected")
            timer.mark("wake_complete")
            return win
        # osascript / iTerm failed -> fall through to headless fallback.
        _audit_wake(conn, wake_id, "window path failed -> headless fallback")

    timer.mark("spawn_marrow")
    try:
        result = caller(note_text, home, resume_sid, cfg)
    except WakeError as e:
        _force_fresh_next(conn, state, today)
        _audit_wake(conn, wake_id, f"wake_failed: {str(e)[:180]}")
        timer.mark("marrow_failed")
        raise
    timer.mark("marrow_returned")

    if result.get("capped"):
        _force_fresh_next(conn, state, today)
        _audit_wake(conn, wake_id,
                    f"token_cap breach total={result.get('total_tokens')} -> fresh")
        timer.mark("capped")
        _render_daybrief(cfg)
        timer.mark("daybrief")
        timer.mark("wake_complete")
        return result

    new_state = replace(
        state,
        cortex_session_id=result.get("session_id") or resume_sid,
    )
    integration.save_state(conn, new_state)

    _render_daybrief(cfg)
    timer.mark("daybrief")
    timer.mark("wake_complete")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual cortex wake entry point (supervised)")
    parser.add_argument("--force", action="store_true", help="bypass pacemaker gates, wake now")
    parser.add_argument("--print-note", action="store_true",
                         help="assemble + print the real wakeup note only, no marrow call")
    args = parser.parse_args(argv)

    cfg = config.load()
    conn = db.connect(cfg)
    try:
        now = _now(cfg)
        if args.print_note:
            text = assemble_note(conn, cfg, now)
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
