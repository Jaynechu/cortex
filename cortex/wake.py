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
                  wake_kind: str | None = None,
                  died_no_handoff: bool = False) -> str:
    """Thin wrapper: gather() + render(). `fresh`/`wake_kind` gate the handoff
    section — only a fresh window (rotate) receives it. `died_no_handoff` adds
    the respawn-catchup line (dead window left no handoff)."""
    data = note.gather(conn, cfg, now, decision=decision,
                       fresh=fresh, wake_kind=wake_kind,
                       died_no_handoff=died_no_handoff)
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


def _alert_respawn_failed(conn: sqlite3.Connection, wake_id: str, detail: str) -> None:
    """The SOLE alert point in the wake ladder: a respawn that failed (exception
    / window did not come up). Writes a marrow `alerts` row (the surfaced alert
    table), falling back to an audit_log row if that table is absent. Best-effort
    — never crashes the pacemaker."""
    try:
        conn.execute(
            "INSERT INTO alerts (severity, type, message, source) VALUES (?, ?, ?, ?)",
            ("warn", "cortex_respawn_failed",
             f"cortex wake respawn failed: {detail}", f"cortex_wake:{wake_id}"),
        )
        conn.commit()
        return
    except Exception:  # noqa: BLE001 - table may be absent; fall back to audit
        pass
    _audit_wake(conn, wake_id, f"respawn_failed: {detail}")


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


def wake_id_of(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


def _window_wake_plan(cfg) -> str:
    """Classify how the resident window should be woken. Three outcomes:
      "fresh"  — a deliberate new brain: the rotate flag is set (night close /
                 explicit rotate / rebirth / token-cap fresh) OR the transcript
                 rolled to a different session since the last wake (a /clear).
                 A brand-new session; the handoff carries context forward.
      "resume" — the window/claude simply DIED (crash / manual close) with NO
                 rotate flag: relaunch `claude --resume <sid>` so the SAME
                 conversation comes back with full context (no handoff catchup).
      "ear"    — the window is alive and unrotated: use the signal-file ear.

    The rotate flag is read-and-cleared here (take_rotated), so this must be
    called exactly once per wake."""
    from cortex import transcript, wake_state, window

    if wake_state.take_rotated(cfg):
        # Clear the stale transcript pointer at the SAME moment the one-shot
        # rotated flag is consumed: from here on this wake is a fresh spawn
        # (set_awake will record the NEW session's transcript once it exists),
        # so nothing in between may read the retiring session's pointer as
        # live. retired_sid (durable, set at rotate time by lie_down/
        # _night_close) is untouched here — it is the belt-and-braces guard
        # every resume path checks even after this one-shot flag is gone.
        wake_state.update(cfg, transcript=None)
        return "fresh"  # deliberate rotate/rebirth/token-cap -> new brain
    sid = wake_state.get_session_id(cfg)
    if not sid or not window.is_running() or not window._session_alive(sid):
        return "resume"  # window died / gone -> bring the same conversation back
    if window.find_claude_pid(cfg) is None:
        return "resume"  # session alive but claude died -> resume same conversation
    prev = wake_state.load(cfg).get("transcript")
    cur = transcript.newest(cfg)
    cur = str(cur) if cur else None
    # A None recorded hint means the last spawn timed out before the new session
    # jsonl appeared (see _spawn_wake). The window is alive with no rotate flag,
    # so treat it as ear (not fresh) — otherwise cur (any current transcript)
    # != None would re-trigger a respawn every tick (the loop this fix removes).
    # Only a real transcript-to-transcript mismatch is a deliberate /clear.
    if prev is None:
        return "ear"
    return "fresh" if cur != prev else "ear"


def _window_rotated(cfg) -> bool:
    """Back-compat boolean: True when the wake needs a new window (fresh or
    resume), False for the ear path. Prefer _window_wake_plan for the
    fresh-vs-resume distinction."""
    return _window_wake_plan(cfg) != "ear"


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
    spawn_ts; None on timeout (record None, never a stale path).

    When prev_path is None (no prior transcript to compare against — the
    common case since the poll routinely times out, see module docstring
    above), `cur_s != prev_path` is trivially true for ANY existing jsonl, so
    the != shortcut is only valid when prev_path is a real path. With prev_path
    None, acceptance must rely solely on fresh_mtime (mtime >= spawn_ts) —
    otherwise the very first poll iteration returns whatever stale jsonl
    happens to already exist (live-confirmed: recorded hint pointed at an old
    session instead of the new window's)."""
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
            accept = fresh_mtime if prev_path is None else (cur_s != prev_path or fresh_mtime)
            if accept:
                return cur_s
        time.sleep(_SPAWN_TRANSCRIPT_POLL_STEP_S)
        waited += _SPAWN_TRANSCRIPT_POLL_STEP_S
    return None


def _spawn_wake(conn, cfg, now, resume: bool = False) -> dict | None:
    """New-window wake. FRESH (resume=False): a brand-new brain whose FIRST
    prompt is the emoji + bell-marker wake prompt (marrow hook detects the
    marker and injects the note). RESUME (resume=True + a recorded claude
    session UUID): relaunch `claude --resume <sid>` with the same baked prompt
    so the SAME conversation returns with full context AND its wake identity —
    used when the window simply died with no rotate flag. Resume with no
    recorded UUID -> fall back to a fresh spawn. Sets the awake marker + lights
    the watchdog. Returns a result dict, or None on window failure (caller ->
    headless).

    The recorded transcript hint must be the NEW session's jsonl — captured only
    after it actually appears (bounded poll) — never the pre-spawn newest, which
    is the OLD session and would drive an endless respawn loop next tick."""
    from cortex import transcript, wake_state, watchdog, window
    from cortex.pacemaker import integration

    resume_sid = window.claude_session_id(cfg) if resume else None
    prev_path = transcript.newest(cfg)
    prev_path = str(prev_path) if prev_path else None
    spawn_ts = time.time()
    try:
        window.respawn(cfg, initial_prompt=window.fresh_initial_prompt(cfg, now),
                       resume_sid=resume_sid)
    except window.WindowError as e:
        _alert_respawn_failed(conn, wake_id_of(now), str(e)[:180])
        return None
    new_path = _wait_new_transcript(cfg, prev_path, spawn_ts)
    wake_state.set_awake(cfg, _latest_wake_log_id(conn), new_path)
    watchdog.spawn(cfg)
    return {"mode": "window", "session_id": None, "text": None}


def _window_wake(conn, cfg, note_text, now, respawn: bool = False) -> dict | None:
    """Interactive wake. `respawn=True` (rotate/rebirth) -> a deliberate FRESH
    brain via the emoji + bell-marker wake prompt (_spawn_wake). `respawn=False` with a DEAD
    resident -> RESUME the same conversation (`claude --resume`), no handoff
    catchup — context is intact. An alive resident window is woken via the
    signal-file ear: write the note file (marrow hook reads it to inject), append
    a bell line its armed Monitor tails, then verify the wake landed (transcript
    mtime grows within ear_timeout_sec).

    Ear-miss ladder (no fresh-respawn-on-miss for an alive window):
      a. alive claude -> TYPE the bell line + rearm suffix into the window; that
         typed prompt flows through the marrow hook (note injected, session
         rearms). Poll again; land -> done.
      b. only a DEAD claude/session -> resume (or fresh-with-catchup on failure).
      c. respawn failure is the sole alert point (handled by the caller).

    Sets the awake marker + lights the watchdog. Returns a result dict, or None
    if the window path failed (caller -> headless). The wake is NOT over here —
    lie_down (self or watchdog proxy) ends it."""
    from cortex import transcript, wake_state, watchdog, window

    # Note file still written before signalling — the marrow hook reads it to
    # inject the full note when it sees the bell marker.
    window.write_note(cfg, note_text)

    # Deliberate fresh brain (rotate/rebirth) -> emoji + bell-marker wake prompt, new session.
    if respawn:
        return _spawn_wake(conn, cfg, now, resume=False)
    # Simply-dead resident (crash/manual close, no rotate flag) -> resume the
    # same conversation with full context (or fresh-with-catchup if unresumable).
    if not _window_alive(cfg):
        return _resume_or_fresh_dead(conn, cfg, now, "dead resident")

    # Alive resident: the signal-file ear path.
    timeout = float(cfg["wake"].get("ear_timeout_sec", 90))
    try:
        before = transcript.mtime(cfg)
        window.append_wake_signal(cfg, now)
        if not _signal_landed(cfg, before, timeout):
            landed = _ear_miss_ladder(conn, cfg, now, timeout)
            if landed is not None:
                return landed
    except window.WindowError:
        return None
    tpath = transcript.newest(cfg)
    wake_state.set_awake(cfg, _latest_wake_log_id(conn),
                         str(tpath) if tpath else None)
    watchdog.spawn(cfg)
    return {"mode": "window", "session_id": None, "text": None}


def _ear_miss_ladder(conn, cfg, now, timeout: float) -> dict | None:
    """Ear miss on a resident window. Ladder:
      a. claude ALIVE -> type the rearm bell line, poll again; land -> ear wake.
      b. claude DEAD  -> resume the same conversation (`claude --resume`). Only
         when no resumable session UUID exists do we fresh-spawn, and only then
         does the died-no-handoff catchup line apply — a successful resume brings
         the context back so no catchup is needed.
    Returns a result dict when a rung completes the wake; None means the alive
    window rearmed but the retyped signal still did not land (caller falls
    through to set_awake as a plain ear wake — the marker is already set)."""
    from cortex import transcript, wake_state, watchdog, window

    if _window_alive(cfg):
        _audit_wake(conn, wake_id_of(now), "ear miss -> rearm (type signal)")
        before = transcript.mtime(cfg)
        if window.type_wake_signal(cfg, now) and _signal_landed(cfg, before, timeout):
            tpath = transcript.newest(cfg)
            wake_state.set_awake(cfg, _latest_wake_log_id(conn),
                                 str(tpath) if tpath else None)
            watchdog.spawn(cfg)
            return {"mode": "window", "session_id": None, "text": None}
        return None  # rearmed but not confirmed -> caller sets awake anyway

    return _resume_or_fresh_dead(conn, cfg, now, "ear miss (claude dead)")


def _resume_or_fresh_dead(conn, cfg, now, why: str) -> dict | None:
    """A dead resident window with NO rotate flag. A resumable claude session
    UUID -> resume (context back, no catchup) UNLESS that UUID was already
    durably retired by a rotate (wake_state.retired_sid) — the one-shot
    `rotated` flag can be consumed by an unrelated wake while a stale
    `transcript` pointer still resolves claude_session_id() to the retired
    session; retired_sid is the belt-and-braces guard that survives that.
    No UUID (or a retired one) -> fresh spawn with the died-no-handoff
    catchup line (only when the window wrote no handoff)."""
    from cortex import wake_state, window

    sid = window.claude_session_id(cfg)
    if sid and sid == wake_state.get_retired_sid(cfg):
        _audit_wake(conn, wake_id_of(now),
                    f"{why}, sid {sid[:8]} already retired -> fresh, not resume")
        sid = None

    if sid:
        _audit_wake(conn, wake_id_of(now), f"{why} -> resume")
        return _spawn_wake(conn, cfg, now, resume=True)

    _audit_wake(conn, wake_id_of(now), f"{why}, no sid -> fresh")
    if not _handoff_written_this_window(cfg):
        catchup_note = assemble_note(conn, cfg, now, died_no_handoff=True)
        window.write_note(cfg, catchup_note)
    return _spawn_wake(conn, cfg, now, resume=False)


def _handoff_written_this_window(cfg) -> bool:
    """True if the handoff file was touched since this (now dead) window woke —
    i.e. it wrote a handoff before dying. Reuses the mtime-vs-awake_since idea
    (watchdog._handoff_written pattern). No awake_since / no handoff -> False."""
    from datetime import datetime
    from cortex import config as _config, wake_state

    since_raw = wake_state.load(cfg).get("awake_since")
    if not since_raw:
        return False
    try:
        since = datetime.fromisoformat(since_raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return False
    handoff = _config.handoff_path(cfg)
    try:
        if not handoff.exists():
            return False
        return handoff.stat().st_mtime >= since and bool(
            handoff.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _window_alive(cfg) -> bool:
    """The RECORDED cortex session is alive: iTerm up, the recorded session UUID
    still exists, AND a `claude` process runs on THAT session's own tty.

    Per-session by construction: liveness is proven only via the recorded
    session's tty (window._claude_on_session_tty), never the cwd-fallback in
    find_claude_pid — otherwise any other claude window opened in cortex_home
    (or a marrow headless `claude -p` run against the same cwd) would falsely
    mark this dead/closed session "alive" and block the tick reconcile from
    resuming it."""
    from cortex import wake_state, window

    sid = wake_state.get_session_id(cfg)
    if not sid or not window.is_running() or not window._session_alive(sid):
        return False
    return window._claude_on_session_tty(cfg, sid)


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

    state = integration.load_state(conn)
    resume_sid = state.cortex_session_id
    timer.mark("state_loaded")

    note_text = assemble_note(conn, cfg, now, decision=decision)
    home = str(config.cortex_home(cfg))
    timer.mark("note")

    # Interactive path (B3v): the resident iTerm window is the cortex body. Only
    # taken for the real wake (default caller) in window mode; explicit `caller`
    # (tests / headless callers) always runs the marrow-subprocess path below.
    if cfg["wake"].get("mode", "window") == "window" and caller is call_marrow_cortex:
        # Classify the wake once (consumes the rotate flag). "fresh" = deliberate
        # new brain (rotate/rebirth): re-assemble the handoff note. "resume" = a
        # window that simply died: relaunch --resume, same context, plain note.
        # "ear" = alive resident, signal-file ear. Only "fresh" respawns a new
        # brain; _window_wake handles resume-vs-fresh for the dead case itself.
        plan = _window_wake_plan(cfg)
        window_text = note_text
        if plan == "fresh":
            window_text = assemble_note(
                conn, cfg, now, decision=decision, fresh=True, wake_kind="rotate")
            timer.mark("rotate_note")
        win = _window_wake(conn, cfg, window_text, now, respawn=(plan == "fresh"))
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
