"""Wake runner (C3): on a pacemaker wake decision, assemble the wakeup note,
call marrow's resumed full-env cortex session, and persist the session_id.
Freshness (a fresh marrow session, no resume_sid) comes only from the
rotate/dead-window detection: a rotated or dead resident window is a new brain
that reads the previous brain's handoff via SessionStart. The night package
(lie_down(mode='night')) forces a rotate, so the next wake is a plain fresh spawn.

marrow lives in its own repo/venv (separate deps) — invoked as a subprocess
against marrow's own venv python rather than imported in-process, so cortex
stays decoupled (Frame: "own project, sibling of marrow").
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
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


# Sentinel: the dead-path did NOT replace the note (so the caller keeps the
# first note's captured cutoff for seeding). Distinct from None, which is a
# valid delivered-note cutoff (the replacement catch-up note had zero eligible
# replay events).
_OMITTED_CUTOFF = object()


def _now(cfg: dict) -> datetime:
    return datetime.now(ZoneInfo(cfg["core"]["timezone"]))


def assemble_note(conn: sqlite3.Connection, cfg: dict, now: datetime,
                  decision: dict | None = None, fresh: bool = False,
                  wake_kind: str | None = None,
                  died_no_handoff: bool = False,
                  return_cutoff: bool = False):
    """Thin wrapper: gather() + render(). `fresh`/`wake_kind` gate the handoff
    section — only a fresh window (rotate) receives it. `died_no_handoff` adds
    the respawn-catchup line (dead window left no handoff).

    `return_cutoff` (default False): return (text, replay_cutoff_ts) instead of
    just text. The cutoff is the replay ts this note was built on, captured at
    assembly so the D6 wake-open seed anchors to exactly what was rendered — not
    a later re-query that could race in an event this note never showed."""
    data = note.gather(conn, cfg, now, decision=decision,
                       fresh=fresh, wake_kind=wake_kind,
                       died_no_handoff=died_no_handoff, consume_kick=True)
    text = note.render(cfg, now, data)
    if return_cutoff:
        return text, data.get("replay_cutoff_ts")
    return text


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
    """Latest PACEMAKER DECISION row (wake=1), never an activation row from a
    different in-flight actor. Scoped on `explanation IS NOT NULL`: only
    run_tick's write_wake_log sets that column (every decision carries one);
    log_activation_wake_row never does. Without this scope, a scheduled wake
    racing an ear/user/ctl activation could adopt the OTHER actor's just-
    inserted row via this "latest wake=1" query — a real adoption hole (the
    activation's own bind would then either double-bind the same row to two
    wakes, or lose it if the activation's compensating cleanup ever deleted
    a row the adopter now depends on)."""
    row = conn.execute(
        "SELECT id FROM ct_wake_log WHERE wake = 1 AND explanation IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def _wake_log_id(conn: sqlite3.Connection, now: datetime,
                 wake_reasons: str | None) -> int | None:
    """The single chokepoint every UNCONDITIONAL set_awake caller uses to bind
    its wake row (fresh spawn / resume / rearm — none of these have a reject
    path, so insert-then-bind can never race a losing epoch check). A
    pacemaker-decided wake (`wake_reasons` None) reuses the decision row
    run_tick already wrote (its latest wake=1 row WITH an explanation — see
    _latest_wake_log_id). Every non-tick wake (user / ctl / reconcile / rotate
    — `wake_reasons` set) writes its OWN activation row so the wakeup note's
    "Last wake" segment counts it. Both feed the same wake_log_id lie_down
    later updates with tokens/force_slept, so accounting is unchanged. Falls
    back to the latest decision row if the fresh insert failed.

    The ear path (the one set_awake call with a real conditional reject,
    expected_gen) does NOT use this — see _bind_wake_log_id, which makes the
    insert + bind one atomic locked step so a losing race never leaves an
    orphan row to clean up."""
    if wake_reasons:
        wid = integration.log_activation_wake_row(conn, now, wake_reasons)
        if wid is not None:
            return wid
    return _latest_wake_log_id(conn)


_BIND_INSERT_BUSY_TIMEOUT_MS = 500


def _resolve_wake_log_id_fast_fail(conn: sqlite3.Connection, now: datetime,
                                   wake_reasons: str | None) -> int | None:
    """The full _wake_log_id sequence (insert-or-reuse), but with the shared
    connection's busy_timeout temporarily dropped to ~500ms for every DB
    statement here (restored after, success or failure, so every OTHER caller
    of the same `conn` keeps the normal 30s default). Used ONLY inside
    _bind_wake_log_id's locked closure — every DB statement there runs while
    _strict_flock is held, and the connection's real busy_timeout is 30s
    (db.py connect_path); under write contention any one of them could hold
    the lock up to 30s, starving competing set_awake/claim_lie_down callers
    (5s deadline) into failing closed.

    codex gate P1: when `wake_reasons` is set (an activation-tagged wake), a
    FAILED insert must NEVER fall through to the decision-row reuse fallback
    (_latest_wake_log_id) — that fallback is only for the scheduled
    (wake_reasons=None) path. Under BEGIN IMMEDIATE contention the fallback
    SELECT can still succeed (it's a read), so falling through would bind this
    wake to an UNRELATED old pacemaker decision row; a later lie_down would
    then overwrite THAT row's tokens/force_slept, corrupting a wake that
    already happened. A failed insert also leaves an implicit transaction open
    on `conn` (Python's sqlite3 begins one on the first DML, even a failing
    one) — rolled back here (best-effort) so no open txn survives this call.

    A contention miss on the activation path returns None outright (skip the
    row this wake — best-effort accounting, never a reason to hold the state
    lock longer or corrupt another wake's row)."""
    try:
        prev_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    except sqlite3.Error:
        prev_timeout = None
    try:
        conn.execute(f"PRAGMA busy_timeout={_BIND_INSERT_BUSY_TIMEOUT_MS}")
        if wake_reasons:
            # log_activation_wake_row catches its own sqlite3.Error and
            # returns None on failure rather than raising, so the failure
            # signal here is the return value, not an exception. Either way,
            # a failed insert must never fall through to the decision-row
            # reuse fallback below (P1) — that reuse is scheduled-wake-only.
            try:
                wid = integration.log_activation_wake_row(conn, now, wake_reasons)
            except sqlite3.Error:
                wid = None
            if wid is None:
                # A failed/attempted insert can leave an implicit transaction
                # open on `conn` (Python's sqlite3 begins one on the first DML,
                # even a failing one) -- roll it back so no open txn survives
                # this call. A no-op if nothing was ever opened.
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
            return wid
        try:
            return _latest_wake_log_id(conn)
        except sqlite3.Error:
            return None
    finally:
        if prev_timeout is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA busy_timeout={int(prev_timeout)}")


def _bind_wake_log_id(conn: sqlite3.Connection, cfg: dict, now: datetime,
                      wake_reasons: str | None, token: tuple[int, str]) -> None:
    """Ear path only: insert/reuse this wake's ct_wake_log row AND bind it into
    wake_state as ONE atomic step under the strict wake_state lock, keyed to
    `token` (the (gen, state_id) set_awake just returned).

    Structural fix (codex gate, 3rd round on this race): a prior version did
    the DB write BEFORE the lock, then tried to bind, then compensated with a
    DELETE on a rejected bind. That had two real holes: (1) a racing scheduled
    wake could ADOPT the just-inserted row via _latest_wake_log_id's unscoped
    "latest wake=1" reuse before our bind ran, so our DELETE removed the row
    the WINNER was now pointing at (lie_down loses that wake's tokens); (2) a
    transient lock failure (StateValidationError also fires on lock timeout /
    unreadable state, not just a stale token) deleted a REAL wake's row that
    was never actually orphaned. Both are closed by making the write part of
    the SAME conditional_mutate call: the closure below only runs (only
    inserts) once the token has already been proven current under the lock, so
    a stale/failed check means NOTHING was ever inserted — no delete needed,
    no adoption window, no tombstone. `_latest_wake_log_id`'s reuse fallback is
    additionally scoped to genuine pacemaker decision rows (explanation IS NOT
    NULL — only run_tick's write_wake_log sets it), so it can never adopt an
    activation row from a different in-flight actor either way.

    4th round (codex gate): the shared connection's busy_timeout is 30s
    (db.connect_path) — any DB statement here held under write contention could
    hold THIS strict lock for up to 30s, starving competing
    set_awake/claim_lie_down (their own 5s deadline) into silently dropping
    real transitions. Every DB statement now runs with a ~500ms busy_timeout
    override (_resolve_wake_log_id_fast_fail): on contention it fails fast,
    the row is simply skipped (wake_log_id stays None on this wake —
    best-effort accounting, same class of degradation as any other
    log_activation_wake_row failure), and the state transition itself
    completes normally. The state machine never waits on the ledger."""
    from cortex import wake_state

    def _insert_and_bind(d: dict) -> None:
        d["wake_log_id"] = _resolve_wake_log_id_fast_fail(conn, now, wake_reasons)

    try:
        wake_state.conditional_mutate(cfg, token, _insert_and_bind)
    except wake_state.StateValidationError:
        pass  # stale token or lock hiccup -> nothing was inserted, no cleanup needed


def wake_id_of(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


def _classify_wake(cfg) -> tuple[str, bool]:
    """Classify how the resident window should be woken, in ONE protected read.
    Returns (plan, rotate_driven):
      plan="fresh"  — a deliberate new brain: EITHER the rotate flag is set
                 (night package / explicit rotate / rebirth / token-cap fresh;
                 rotate_driven=True) OR the transcript rolled to a different
                 session since the last wake (a /clear; rotate_driven=False).
                 A brand-new session; the handoff (or died_no_handoff catchup)
                 carries context forward.
      plan="resume" — the window/claude simply DIED (crash / manual close) with NO
                 rotate flag: relaunch `claude --resume <sid>` so the SAME
                 conversation comes back with full context (no handoff catchup).
                 A resume attempt that fails to land falls back to "fresh" with
                 the died_no_handoff catchup — see _window_wake — so a dead
                 window always ends in a live awake cortex, never nothing.
      plan="ear"    — the window is alive and unrotated: use the signal-file ear.

    rotate_driven is TRUE only when THIS SAME call observed the rotate flag set
    -- it is never re-derived from a second, later peek_rotated() call (codex
    adversarial-review Fix 1: a second read outside the lock left a window where
    the winner's take_rotated() landed between this call's plan and a later
    re-peek, so a rotate loser kept plan=="fresh" with rotate_driven appearing
    False and bypassed the concurrent-rotate skip guard -> a second fresh spawn).
    The caller (run_wake) must call this EXACTLY ONCE per wake, under the spawn
    lock (_spawn_serialized), and carry the returned rotate_driven through to both
    the skip guard and the deferred take_rotated consume -- never peek again.

    The rotate flag itself is PEEKED here (peek_rotated), never consumed: Fix 1
    defers the one-shot consume (take_rotated) to AFTER the fresh successor is
    verified live, so a failed spawn keeps the flag for the retry."""
    from cortex import transcript, wake_state, window

    if wake_state.peek_rotated(cfg):
        # Clear the stale transcript pointer while the rotate flag still stands:
        # from here on this wake is a fresh spawn (set_awake records the NEW
        # session's transcript once it exists), so nothing in between may read the
        # retiring session's pointer as live. Idempotent (already None on a retry).
        # retired_sid (durable, set at rotate time by lie_down) is untouched here
        # -- it is the belt-and-braces guard every resume path checks even after
        # the one-shot flag is finally consumed.
        wake_state.update(cfg, transcript=None)
        return "fresh", True  # deliberate rotate/rebirth/token-cap -> new brain
    sid = wake_state.get_session_id(cfg)
    if not sid or not window.is_running() or not window._session_alive(sid):
        return "resume", False  # window died/gone -> bring the same conversation back
    if window.find_claude_pid(cfg) is None:
        return "resume", False  # session alive but claude died -> resume same conversation
    prev = wake_state.load(cfg).get("transcript")
    cur = transcript.newest(cfg)
    cur = str(cur) if cur else None
    # A None recorded hint means the last spawn timed out before the new session
    # jsonl appeared (see _spawn_wake). The window is alive with no rotate flag,
    # so treat it as ear (not fresh) — otherwise cur (any current transcript)
    # != None would re-trigger a respawn every tick (the loop this fix removes).
    # Only a real transcript-to-transcript mismatch is a deliberate /clear.
    if prev is None:
        return "ear", False
    return ("fresh", False) if cur != prev else ("ear", False)


def _window_wake_plan(cfg) -> str:
    """Back-compat/standalone wrapper: the plan string alone, for callers (tests,
    _window_rotated) that only need the fresh/resume/ear classification, not the
    rotate_driven distinction run_wake's lock-protected dispatch requires. Safe to
    call more than once (peek is idempotent) -- but run_wake itself must use
    _classify_wake exactly once, under the lock (see its docstring)."""
    return _classify_wake(cfg)[0]


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
# first turn, so newest() right after respawn returns a PRE-EXISTING session's
# file — recording that stale hint makes _window_rotated see a mismatch on the
# next tick and respawn forever (the P0 loop). Snapshot the dir's *.jsonl names
# BEFORE spawn; poll until a file OUTSIDE that snapshot shows up (the new window
# is by definition created after spawn); on timeout record None so
# _window_rotated's None-hint guard keeps the alive window unrotated.
_SPAWN_TRANSCRIPT_POLL_STEP_S = 0.5
_SPAWN_TRANSCRIPT_POLL_TIMEOUT_S = 8.0


def _wait_new_transcript(cfg, preexisting: set[str]) -> str | None:
    """Poll (bounded) for the NEW session transcript after a spawn. Accepts a
    jsonl iff its name is NOT in `preexisting` — the set of *.jsonl filenames
    snapshotted from the transcript dir BEFORE the spawn launched. Returns the
    newest such file (by mtime); None on timeout (record None, never a stale
    path).

    Filesystem, not ledger: the new window's jsonl is by definition created
    after the spawn, so it is absent from the pre-spawn snapshot. The retiring
    window's file — still being written for seconds after spawn (lie_down MCP
    return + its final turn), hence mtime-newest — is by definition IN the
    snapshot, so it is skipped no matter what the ledger recorded. The directory
    is scanned for files outside the snapshot (not transcript.newest, which
    during the race IS the retiring file); the newest of those is returned."""
    from cortex import transcript

    d = transcript.transcript_dir(cfg)
    waited = 0.0
    while waited < _SPAWN_TRANSCRIPT_POLL_TIMEOUT_S:
        if d.exists():
            fresh = [p for p in d.glob("*.jsonl") if p.name not in preexisting]
            if fresh:
                newest = max(fresh, key=lambda p: p.stat().st_mtime)
                return str(newest)
        time.sleep(_SPAWN_TRANSCRIPT_POLL_STEP_S)
        waited += _SPAWN_TRANSCRIPT_POLL_STEP_S
    return None


def _wait_resume_transcript(cfg, resume_sid: str, before: tuple[float, int] | None,
                            preexisting: set[str]) -> str | None:
    """Poll (bounded) for evidence that a `claude --resume` landed. Unlike a fresh
    spawn, --resume APPENDS to the existing <resume_sid>.jsonl — no new file
    appears — so waiting on a new-file snapshot burns the whole timeout and wipes
    the transcript hint. Settle on evidence instead: the resume file's (mtime,
    size) growing past `before` (captured pre-spawn; None = file absent then),
    returning that path. Edge: --resume can silently degrade to a fresh session
    with a NEW sid; if a jsonl outside `preexisting` appears, fresh-file evidence
    wins (settle on it). None on timeout (neither grew nor appeared)."""
    from cortex import transcript

    d = transcript.transcript_dir(cfg)
    rp = d / f"{resume_sid}.jsonl"
    waited = 0.0
    while waited < _SPAWN_TRANSCRIPT_POLL_TIMEOUT_S:
        if d.exists():
            fresh = [p for p in d.glob("*.jsonl") if p.name not in preexisting]
            if fresh:
                return str(max(fresh, key=lambda p: p.stat().st_mtime))
        try:
            st = rp.stat()
            now_ev = (st.st_mtime, st.st_size)
        except OSError:
            now_ev = None
        if now_ev is not None and (before is None or now_ev > before):
            return str(rp)
        time.sleep(_SPAWN_TRANSCRIPT_POLL_STEP_S)
        waited += _SPAWN_TRANSCRIPT_POLL_STEP_S
    return None


def _spawn_wake(conn, cfg, now, resume: bool = False,
                wake_reasons: str | None = None) -> dict | None:
    """New-window wake. FRESH (resume=False): a brand-new brain whose FIRST
    prompt is the emoji + bell-marker wake prompt (marrow hook detects the
    marker via the wake_state receipt and injects the note). RESUME (resume=True
    + a recorded claude session UUID): relaunch `claude --resume <sid>` with NO
    baked prompt and NO receipt — the conversation IS the identity and the CC
    harness's own background-shell notice (the resumed session's armed ear
    Monitor tail has no completion record) drives the model to take a turn on its
    own. A minimal safety net (Fix 3): if that self-driven turn never appears
    within the bounded readiness window (the prior session died with no armed
    Monitor, so the harness had nothing to report), type ONE ordinary bell line
    (ear-style, machine-tagged, epoch-token in its receipt) so the resumed window
    still gets its note. Resume with no recorded UUID -> fall back to a fresh
    spawn. Sets the awake marker + lights the watchdog. Returns a result dict, or
    None on window failure (caller -> _resume_or_fresh_dead retries as
    fresh-with-catchup on a resume failure, _window_wake -> headless on a fresh
    failure).

    The epoch token is captured before the spawn only to stamp the wake receipt
    (marrow's hook validates it via wake_token_current). The set_awake commit
    itself is UNCONDITIONAL (Fix 4 CAS removed): the physically-up window IS the
    resident; a bell or user message racing ahead of the slow startup must never
    cancel the registration.

    Session id commit (Fix 2): window.respawn() only VERIFIES readiness and
    returns the new sid -- it no longer persists it. The sid is committed here,
    IN THE SAME atomic section as the awake flip (set_awake's session_id= param).

    Resume fallback-bell ordering (Fix 3): the assistant-line baseline is
    captured BEFORE `claude --resume` is launched (a harness-driven turn written
    during launch/readiness must count as growth, not get absorbed into the
    baseline), the awake flip commits immediately once the window is verified
    ready (so a harness turn landing during the fallback-bell poll sees the
    session already marked awake, not asleep), and only THEN does the bell poll
    run, comparing against that pre-launch baseline.

    The recorded transcript hint must be the NEW session's jsonl — captured only
    after it actually appears (bounded poll) — never the pre-spawn newest, which
    is the OLD session and would drive an endless respawn loop next tick."""
    from cortex import transcript, wake_state, watchdog, window

    resume_sid = window.claude_session_id(cfg) if resume else None
    # Snapshot the transcript dir's *.jsonl names BEFORE launching. The new
    # window's jsonl is by definition created after spawn, so it is the file
    # absent from this set; the retiring window's file (still being written for
    # seconds past launch, hence mtime-newest) is by definition IN the set and
    # is skipped — filesystem truth, independent of any ledger field.
    tdir = transcript.transcript_dir(cfg)
    preexisting = {p.name for p in tdir.glob("*.jsonl")} if tdir.exists() else set()
    # Capture the epoch token only to stamp the wake receipt (marrow's hook
    # validates it via wake_token_current). Capture failure (lock/parse) ->
    # receipt carries no token.
    try:
        token = wake_state.current_epoch(cfg)
    except wake_state.StateValidationError:
        token = None
    if resume_sid:
        # Resume: clean launch. The conversation is the identity; no bell typed,
        # no receipt written -- the harness's own background-shell notice wakes
        # the model. The Fix-3 fallback bell (below) is the only thing that ever
        # types into a resumed window, and only when no self-driven turn appears.
        initial_prompt = None
        # Fix 3: baseline BEFORE launch, so a harness-driven turn written during
        # the resume/readiness window (which can legitimately take real seconds)
        # counts as growth against this baseline, never gets silently absorbed by
        # capturing the baseline only after the window already came up.
        resume_baseline = _assistant_line_count(cfg, resume_sid)
        # Pre-spawn (mtime, size) of the resume file: --resume APPENDS to it (no
        # new file appears), so growth past this is the settle evidence. None =
        # absent now (any later stat counts as growth).
        try:
            _rst = (tdir / f"{resume_sid}.jsonl").stat()
            resume_before = (_rst.st_mtime, _rst.st_size)
        except OSError:
            resume_before = None
    else:
        # Fresh: the visible bell is the first prompt; write its receipt (with the
        # epoch token, Fix 4) BEFORE spawning so the marrow hook recognizes the
        # new window's first line AND suppresses it if a newer epoch superseded.
        window.write_wake_receipt(cfg, now, token=token)
        initial_prompt = window.fresh_initial_prompt(cfg, now)
        resume_baseline = None
    try:
        new_sid = window.respawn(cfg, initial_prompt=initial_prompt, resume_sid=resume_sid)
    except window.WindowError as e:
        _alert_respawn_failed(conn, wake_id_of(now), str(e)[:180])
        return None
    if resume_sid:
        # Resume APPENDS to the existing <resume_sid>.jsonl -> no new file, so
        # _wait_new_transcript would burn its timeout and wipe the hint. Settle on
        # growth evidence instead (fresh-file wins if --resume degraded to a new
        # sid). See _wait_resume_transcript.
        new_path = _wait_resume_transcript(cfg, resume_sid, resume_before, preexisting)
    else:
        new_path = _wait_new_transcript(cfg, preexisting)
    # ONE atomic commit -- awake flip, wake_log_id, transcript hint, AND the new
    # resident session id (Fix 2), WITHOUT bumping the epoch. bump=False keeps
    # live gen == the receipt's gen so the marrow hook's equality check
    # (wake_token_current) processes the bell instead of suppressing it as
    # stale. UNCONDITIONAL (Fix 4 CAS removed): the physically-up window is the
    # resident, whatever raced past the epoch during the slow startup.
    from pathlib import Path
    claude_sid = Path(new_path).stem if new_path else None
    wake_state.set_awake(
        cfg, _wake_log_id(conn, now, wake_reasons), new_path,
        bump=False, session_id=new_sid, cortex_claude_sid=claude_sid)
    watchdog.spawn(cfg)
    if resume_sid:
        # Fix 3: the awake flip is already committed; the fallback-bell poll now
        # only decides whether a nudge is needed, comparing against the PRE-LAUNCH
        # baseline captured above (never re-captured after launch).
        _resume_fallback_bell(cfg, now, token, resume_sid, resume_baseline)
    return {"mode": "window", "session_id": None, "text": None}


_RESUME_TURN_POLL_STEP_S = 3.0


def _assistant_line_count(cfg, resume_sid: str) -> int:
    """Count assistant-role entries in the resumed conversation's transcript
    jsonl (<transcript_dir>/<resume_sid>.jsonl). Top-level `type == "assistant"`
    is the model-turn marker (same shape window_tokens reads). A missing/
    unreadable file -> 0. Used to detect a NEW model turn after a resume WITHOUT
    relying on mtime (hook writes touch mtime -> false positive, observed live)."""
    from cortex import transcript

    p = transcript.transcript_dir(cfg) / f"{resume_sid}.jsonl"
    try:
        n = 0
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and o.get("type") == "assistant":
                    n += 1
        return n
    except OSError:
        return 0


def _resume_fallback_bell(cfg, now, token, resume_sid: str, baseline: int) -> None:
    """Fix 3 safety net: a resumed window normally wakes itself via the CC
    harness's own background-shell notice (its armed ear Monitor had no
    completion record) -- that alone drives a full model turn. But if the prior
    session died with NO armed Monitor, the harness has nothing to report and the
    resumed model sits idle. Poll the resumed conversation's transcript for a NEW
    assistant-role line (NOT mtime -- hook writes touch mtime, a false positive
    observed live) for up to resume_turn_timeout_sec (default 180s: a resume
    replays a long conversation first and the harness-triggered turn alone can
    churn 85s+). If a new assistant line appears, do nothing (no bell, no
    receipt). On timeout with no new model turn, type ONE ordinary bell line
    (ear-style, machine-tagged, via type_wake_signal) so the resumed window still
    gets its note. The bell carries the epoch token in its receipt (Fix 4) like
    the ear path, so a superseded wake is suppressed by the marrow hook.

    `baseline` MUST be the assistant-line count captured BEFORE `claude --resume`
    was launched (codex adversarial-review Fix 3): capturing it here, after launch
    +readiness, let a harness-driven turn written during that (real, multi-second)
    window land inside the baseline itself, so growth was never observed and the
    bell fired at the full timeout regardless -- duplicating the wake every time.
    The caller (_spawn_wake) also commits the awake flip BEFORE calling this, so a
    harness turn landing during this poll sees the session already marked awake
    (a wait()/lie_down() call mid-poll is not wrongly rejected as "not awake")."""
    from cortex import window

    timeout = float(cfg["wake"].get("resume_turn_timeout_sec", 180))
    waited = 0.0
    while waited < timeout:
        time.sleep(min(_RESUME_TURN_POLL_STEP_S, timeout - waited))
        waited += _RESUME_TURN_POLL_STEP_S
        if _assistant_line_count(cfg, resume_sid) > baseline:
            return  # the harness's own notice already drove a model turn
    try:
        window.type_wake_signal(cfg, now, token=token)
    except window.WindowError:
        pass


def _window_wake(conn, cfg, note_text, now, respawn: bool = False,
                 wake_reasons: str | None = None) -> dict | None:
    """Interactive wake. `respawn=True` (rotate/rebirth) -> a deliberate FRESH
    brain via the emoji + bell-marker wake prompt (_spawn_wake). `respawn=False`
    with a DEAD resident -> RESUME the same conversation (`claude --resume`), no
    handoff catchup — context is intact; a resume attempt that fails to land
    falls back to fresh-with-catchup (_resume_or_fresh_dead), so a dead window
    always ends in a live awake cortex, never nothing. An alive resident window
    is woken via the signal-file ear: write the note file (marrow hook reads it
    to inject), append a bell line its armed Monitor tails, then verify the wake
    landed (transcript mtime grows within ear_timeout_sec).

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
        return _spawn_wake(conn, cfg, now, resume=False, wake_reasons=wake_reasons)
    # Simply-dead resident (crash/manual close, no rotate flag) -> resume the
    # same conversation with full context (or fresh-with-catchup if unresumable
    # or the resume spawn itself fails). Defense-in-depth: the plan classified
    # this as "ear" (alive+unrotated), but the window can die in the gap between
    # that check and here — re-check before trying to signal a dead resident.
    if not _window_alive(cfg):
        return _resume_or_fresh_dead(conn, cfg, now, "dead resident",
                                     wake_reasons=wake_reasons)

    # Alive resident: the signal-file ear path. Capture the SLEEPING epoch first
    # so the wake line carries it and set_awake is conditional on it: if a user
    # message flips awake + bumps gen between here and set_awake, the conditional
    # flip loses (the user reset already woke the window) — no double activation.
    timeout = float(cfg["wake"].get("ear_timeout_sec", 90))
    try:
        sleep_gen, sleep_sid = wake_state.current_epoch(cfg)
    except wake_state.StateValidationError:
        return None
    token = (sleep_gen, sleep_sid)
    try:
        before = transcript.mtime(cfg)
        window.append_wake_signal(cfg, now, token=token)
        if not _signal_landed(cfg, before, timeout):
            landed = _ear_miss_ladder(conn, cfg, now, timeout,
                                      wake_reasons=wake_reasons)
            if landed is not None:
                return landed
    except window.WindowError:
        return None
    tpath = transcript.newest(cfg)
    # Wake-row commit AFTER the conditional set_awake succeeds (P2 fix): this is
    # the one set_awake call with a real reject path (expected_token — a user
    # message flipping awake first between the ear signal and here). Writing
    # the activation row BEFORE the transition is known to succeed would leave
    # a phantom row (the row belongs to a wake that never actually happened,
    # while the user's own wake gets its own row) when set_awake loses the race.
    # Fix 4: pass the FULL (gen, state_id) token (already captured as `token`
    # above) rather than gen alone -- a gen-only check tolerates a wake_state.json
    # delete/recreate landing back on the same gen with a new state_id.
    new_epoch = wake_state.set_awake(cfg, None, str(tpath) if tpath else None,
                                     expected_token=token)
    if new_epoch is not None:
        _bind_wake_log_id(conn, cfg, now, wake_reasons, new_epoch)
    watchdog.spawn(cfg)
    return {"mode": "window", "session_id": None, "text": None}


def _ear_miss_ladder(conn, cfg, now, timeout: float,
                     wake_reasons: str | None = None) -> dict | None:
    """Ear miss on a resident window. Ladder:
      a. claude ALIVE -> type the rearm bell line, poll again; land -> ear wake.
      b. claude DEAD  -> resume the same conversation (`claude --resume`). Only
         when no resumable session UUID exists, or the resume spawn itself
         fails, do we fall back to a fresh spawn with the died-no-handoff
         catchup line.
    Returns a result dict when a rung completes the wake; None means the alive
    window rearmed but the retyped signal still did not land (caller falls
    through to set_awake as a plain ear wake — the marker is already set)."""
    from cortex import transcript, wake_state, watchdog, window

    if _window_alive(cfg):
        _audit_wake(conn, wake_id_of(now), "ear miss -> rearm (type signal)")
        before = transcript.mtime(cfg)
        if window.type_wake_signal(cfg, now) and _signal_landed(cfg, before, timeout):
            tpath = transcript.newest(cfg)
            wake_state.set_awake(cfg, _wake_log_id(conn, now, wake_reasons),
                                 str(tpath) if tpath else None)
            watchdog.spawn(cfg)
            return {"mode": "window", "session_id": None, "text": None}
        return None  # rearmed but not confirmed -> caller sets awake anyway

    return _resume_or_fresh_dead(conn, cfg, now, "ear miss (claude dead)",
                                 wake_reasons=wake_reasons)


def _resume_or_fresh_dead(conn, cfg, now, why: str,
                          wake_reasons: str | None = None) -> dict | None:
    """A dead resident window with NO rotate flag. A resumable claude session
    UUID -> resume (context back, no catchup) UNLESS that UUID was already
    durably retired by a rotate (wake_state.retired_sid) — the one-shot
    `rotated` flag can be consumed by an unrelated wake while a stale
    `transcript` pointer still resolves claude_session_id() to the retired
    session; retired_sid is the belt-and-braces guard that survives that.

    Contract: after this returns, either the caller has a live awake cortex, or
    the wake fell all the way through to run_wake's headless fallback — never
    silently nothing. A resume ATTEMPT that fails to land (the resume spawn
    itself returns None — bad/gone sid, claude errors out, window doesn't come
    up) is NOT the end of the road: it is retried once as a fresh spawn with the
    died-no-handoff catchup line, same as the no-UUID case, so resume is
    preferred but fresh is always the fallback, never a dead end.

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
        result = _spawn_wake(conn, cfg, now, resume=True, wake_reasons=wake_reasons)
        if result is not None:
            return result
        # Resume spawn failed to land (already alerted by _spawn_wake) -> the
        # window must never end up with nothing. Retry once as fresh-with-
        # catchup, same as the no-UUID case below.
        _audit_wake(conn, wake_id_of(now), f"{why}, resume failed -> fresh fallback")

    return _fresh_dead_spawn(conn, cfg, now, wake_reasons=wake_reasons)


def _fresh_dead_spawn(conn, cfg, now, wake_reasons: str | None = None) -> dict | None:
    """Fresh-spawn a new brain for a dead resident (no rotate flag): the
    died-no-handoff catchup line is added when the previous window wrote no
    handoff (a crash/close, not a deliberate rotate) so the fresh brain
    recovers context from the transcript."""
    from cortex import window

    delivered_cutoff = _OMITTED_CUTOFF
    if not _handoff_written_this_window(cfg):
        # A second note is assembled and DELIVERED here (replacing the first note
        # written before _window_wake was entered). Seeding must anchor to THIS
        # note's cutoff, not the first's — else an event arriving between the two
        # assemblies is shown here yet stays > the first-note baseline and gets
        # duplicated in the first free-round (#3). Propagate the delivered cutoff.
        catchup_note, delivered_cutoff = assemble_note(
            conn, cfg, now, died_no_handoff=True, return_cutoff=True)
        window.write_note(cfg, catchup_note)
    result = _spawn_wake(conn, cfg, now, resume=False, wake_reasons=wake_reasons)
    if result is not None and delivered_cutoff is not _OMITTED_CUTOFF:
        result["note_cutoff"] = delivered_cutoff
    return result


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


@contextlib.contextmanager
def _spawn_serialized(cfg: dict):
    """Exclusive flock serialising EVERY window-spawn entrant (pacemaker tick
    reconcile's _fire_dead_window, ctl wake's no-resident branch, rotate
    succession) — modeled on watchdog._spawn_lock (same shape: held across
    check + spawn, no persistent state so a crash mid-spawn self-heals; the
    advisory flock releases on ANY process exit).

    07-20 live race: a SIGKILLed resident (simulated crash) + `ctl wake` both
    ran within the same ~5s window. Both passed the "no resident" liveness
    check (_window_alive) BEFORE either spawned — the checks and the actual
    window spawn were two separate, unlocked steps, so both callers proceeded
    and landed two identical resume windows. Locking this whole check+spawn
    section closes that: the loser's re-check (inside the lock, after acquiring
    it) sees the winner's now-live window and skips."""
    from cortex import wake_state

    lp = wake_state.spawn_lock_path(cfg)
    lp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


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

    note_text, note_cutoff = assemble_note(
        conn, cfg, now, decision=decision, return_cutoff=True)
    home = str(config.cortex_home(cfg))
    timer.mark("note")

    # Interactive path (B3v): the resident iTerm window is the cortex body. Only
    # taken for the real wake (default caller) in window mode; explicit `caller`
    # (tests / headless callers) always runs the marrow-subprocess path below.
    if cfg["wake"].get("mode", "window") == "window" and caller is call_marrow_cortex:
        from cortex import wake_state
        # Wake-row reasons: a pacemaker-decided wake (scheduled) already has its
        # decision row from run_tick -> reuse it (None). A non-tick wake (ctl /
        # reconcile / user) carries an explicit tag in decision["wake_reasons"]
        # so the chokepoint logs a fresh activation row (BUG A: those wakes wrote
        # no wake=1 row, so "Last wake" skipped every real wake since noon).
        wake_reasons = decision.get("wake_reasons")

        # codex adversarial-review Fix 1: classification (_classify_wake) now
        # happens EXACTLY ONCE, INSIDE the spawn lock, and its rotate_driven
        # result is carried straight through -- never re-derived via a second,
        # later peek_rotated() call. The prior version classified OUTSIDE the
        # lock, then sampled peek_rotated() a second time for rotate_claim: an
        # entrant pausing between those two reads while the winner's
        # take_rotated() landed in between kept plan=="fresh" with rotate_claim
        # appearing False (the flag already gone) -> bypassed the loser guard
        # -> a second fresh spawn, two residents. Wrapping classify+dispatch in
        # ONE lock hold removes the gap entirely: "ear" also moves inside (no
        # separate un-serialized fast path), since a second classification call
        # for "ear" was exactly the reintroduced race window.
        with _spawn_serialized(cfg):
            plan, rotate_driven = _classify_wake(cfg)
            window_text = note_text
            window_cutoff = note_cutoff
            if plan == "fresh":
                window_text, window_cutoff = assemble_note(
                    conn, cfg, now, decision=decision, fresh=True,
                    wake_kind="rotate", return_cutoff=True)
                timer.mark("rotate_note")
            if plan == "ear":
                win = _window_wake(conn, cfg, window_text, now, respawn=False,
                                   wake_reasons=wake_reasons)
            elif plan == "resume" and _window_alive(cfg):
                # Re-check liveness under the lock before spawning a RESUME: the
                # winner's _spawn_wake records the new session (set_awake) before
                # releasing this lock, so a loser arriving after sees
                # _window_alive()=True and skips (07-20 live race: pacemaker tick
                # reconcile and ctl wake both passed the "no resident" check
                # before either spawned, landing two identical resume windows).
                win = {"mode": "window", "session_id": None, "text": None,
                      "skipped": "spawn_race_lost"}
            elif rotate_driven and not wake_state.peek_rotated(cfg):
                # Defense-in-depth only (should be unreachable under the
                # single-classify-under-lock structure above): rotate_driven came
                # from THIS SAME classification call, so the flag it observed is
                # still this entrant's to claim -- no other holder of this lock
                # could have consumed it in between. Kept as a belt-and-braces
                # guard, never the primary defense.
                win = {"mode": "window", "session_id": None, "text": None,
                      "skipped": "spawn_race_lost"}
            else:
                win = _window_wake(conn, cfg, window_text, now,
                                   respawn=(plan == "fresh"),
                                   wake_reasons=wake_reasons)
                # Consume the rotate flag ONLY now the fresh successor is live
                # (rotate-driven fresh, win a real result dict -- not None/skip).
                # A None (window failure) or skip leaves the flag set for the
                # retry to own; a /clear-driven fresh has no flag to consume.
                if (rotate_driven and win is not None
                        and not win.get("skipped")):
                    wake_state.take_rotated(cfg)
        if win is not None and win.get("skipped") == "spawn_race_lost":
            # The winner already seeded the baseline / marked awake — this
            # entrant touches nothing further, just reports the outcome.
            timer.mark("wake_complete")
            return win
        if win is not None:
            # D6 seed: set_awake (inside _window_wake) just reset last_note_ts to
            # None. Anchor the diff-mode baseline to the cutoff captured when the
            # DELIVERED note was assembled (P2-A) — not a fresh query after the
            # ~90s window spawn, which would race in an event absent from the note
            # and drop it from the first free-round.
            #
            # The dead-window path may REPLACE the first note with a second
            # died_no_handoff catch-up note; when it does it reports that note's
            # cutoff via win["note_cutoff"] (may be None = empty replay). Seed from
            # the delivered note's cutoff so an event arriving between the two
            # assemblies is not duplicated in the first free-round (#3).
            seed_cutoff = win["note_cutoff"] if "note_cutoff" in win else window_cutoff
            note.seed_baseline(conn, cfg, cutoff_ts=seed_cutoff)
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

    # Headless wakes (true headless mode, or the window path failing over to
    # it) never touch wake_state.set_awake -> the ctl/reconcile/--force chain
    # that tags decision["wake_reasons"] wrote no row here, so "Last wake"
    # skipped every one of them too. A pacemaker-decided wake (wake_reasons
    # None) already has run_tick's decision row -> no second write.
    if decision.get("wake_reasons"):
        _wake_log_id(conn, now, decision["wake_reasons"])

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
                        "wake_reasons": "ctl",
                        "explanation": f"{now.strftime('%H:%M')} manual --force wake"}
            run_wake(conn, cfg, decision, now=now)
            return 0
        parser.print_help()
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
