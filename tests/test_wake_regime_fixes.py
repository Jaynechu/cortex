"""Post-registration-deletion hardening (5 fixes):

  Fix 1 - rotate flag consumed only AFTER the fresh successor is verified live;
          preserved on every failure path; claim+spawn serialized.
  Fix 2 - resume readiness returns a verified result or raises WindowError; a
          readiness timeout no longer looks like success.
  Fix 3 - a resumed wake types ONE machine-tagged bell only when no new model
          turn (assistant line) appears within resume_turn_timeout_sec.
  Fix 4 - REMOVED (2026-07-20): the spawn-path set_awake CAS is gone; the
          physically-up window is the resident, unconditionally.
  Fix 5 - the wake note opens with a config-driven machine-origin tag.

No iTerm/osascript here; window control is stubbed. Temp cortex_home + temp DB.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from cortex import config, db, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    dbfile = tmp_path / "marrow.db"
    c = config.load(path=tmp_path / "no-such.toml")  # pure defaults
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(dbfile)
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    # These two default to the LIVE ~/.config/marrow/ dir (conftest isolation
    # guard fails otherwise) -- pin them under tmp_path.
    c["paths"]["wake_timing_log"] = str(tmp_path / "wake_timing.log")
    c["paths"]["handoff_file"] = str(home / "handoff.md")
    return c


def _seed_wake_row(cfg, tag="fix") -> int:
    conn = db.connect(cfg)
    try:
        conn.execute(
            "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
            (db.utcnow_iso(), tag))
        conn.commit()
        return conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    finally:
        conn.close()


# ── Fix 1: rotate flag preserved on failure, consumed only after success ──────

def test_peek_rotated_is_non_destructive(cfg):
    """peek_rotated reads the flag without consuming it (unlike take_rotated)."""
    wake_state.set_rotated(cfg)
    assert wake_state.peek_rotated(cfg) is True
    assert wake_state.peek_rotated(cfg) is True          # still set
    assert wake_state.take_rotated(cfg) is True           # now consumed
    assert wake_state.peek_rotated(cfg) is False


def test_window_wake_plan_fresh_does_not_consume_rotate(cfg, monkeypatch):
    """Fix 1: classification only PEEKS the rotate flag -- it must survive the
    plan call so a failed spawn keeps retry ownership."""
    from cortex import wake

    wake_state.set_rotated(cfg)
    assert wake._window_wake_plan(cfg) == "fresh"
    assert wake_state.peek_rotated(cfg) is True   # NOT consumed by classification


def test_run_wake_fresh_spawn_failure_preserves_rotate_flag(cfg, monkeypatch):
    """Fix 1 core: a rotate wake whose fresh spawn FAILS (window never comes up)
    must leave the rotate flag SET, so the next wake still classifies as fresh
    and never reactivates the retired conversation. Before the fix the flag was
    cleared during classification, before the spawn, so a failed spawn dropped
    it and the retired window got resumed on the next tick."""
    from cortex import note, symlinks, wake, window

    _seed_wake_row(cfg, "rot-fail")
    wake_state.set_rotated(cfg)

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(note, "seed_baseline", lambda *a, **k: None)
    monkeypatch.setattr(wake, "_render_daybrief", lambda c: None)

    # The fresh spawn fails to come up (osascript/iTerm WindowError); the window
    # path then falls through to the headless marrow caller, which is stubbed so
    # no real claude subprocess runs.
    def boom(c, initial_prompt=None, resume_sid=None):
        raise window.WindowError("no iterm")
    monkeypatch.setattr(window, "respawn", boom)
    monkeypatch.setattr(wake, "call_marrow_cortex",
                        lambda *a, **k: {"text": None, "session_id": None})

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        # run_wake's window branch runs only when `caller is call_marrow_cortex`;
        # pass the (now-stubbed) default so the interactive path is exercised.
        wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc),
                      caller=wake.call_marrow_cortex)
    finally:
        conn.close()

    assert wake_state.peek_rotated(cfg) is True  # flag preserved for the retry


def test_run_wake_fresh_spawn_success_consumes_rotate_flag(cfg, monkeypatch):
    """Fix 1: once the fresh successor is verified live, the one-shot rotate flag
    IS consumed (so the wake after it is not another needless respawn)."""
    from cortex import note, symlinks, transcript, wake, watchdog, window

    _seed_wake_row(cfg, "rot-ok")
    wake_state.set_rotated(cfg)

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(note, "seed_baseline", lambda *a, **k: None)
    monkeypatch.setattr(wake, "_render_daybrief", lambda c: None)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc),
                      caller=wake.call_marrow_cortex)
    finally:
        conn.close()

    assert wake_state.peek_rotated(cfg) is False  # consumed after a live successor


def test_run_wake_concurrent_rotate_second_entrant_skips(cfg, monkeypatch):
    """Fix 1 concurrent-rotate (codex adversarial-review hardening): classify +
    dispatch now happen in ONE lock-protected call (_classify_wake), so there is
    no longer a second, later peek_rotated() sample that could observe a
    different answer than the classification itself. Modelled directly: an
    entrant whose _classify_wake call returns rotate_driven=True, but a
    concurrent winner (simulated) has ALREADY consumed the flag by the time the
    belt-and-braces in-lock guard checks peek_rotated() -> this entrant still
    must skip rather than double-spawn (the guard is defense-in-depth even
    though the single-classify structure makes the gap unreachable in practice)."""
    from cortex import note, symlinks, wake, watchdog, window

    _seed_wake_row(cfg, "rot-concurrent")

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(note, "seed_baseline", lambda *a, **k: None)
    monkeypatch.setattr(wake, "_render_daybrief", lambda c: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    spawns = {"n": 0}
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: spawns.__setitem__("n", spawns["n"] + 1))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")

    # ONE classification call (inside the lock) reports rotate_driven=True...
    monkeypatch.setattr(wake, "_classify_wake", lambda c: ("fresh", True))
    # ...but a concurrent winner (simulated externally) already consumed the flag
    # by the time the in-lock belt-and-braces guard checks it.
    monkeypatch.setattr(wake_state, "peek_rotated", lambda c: False)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        res = wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc),
                            caller=wake.call_marrow_cortex)
    finally:
        conn.close()

    assert spawns["n"] == 0                       # loser skipped the fresh spawn
    assert res.get("skipped") == "spawn_race_lost"


def test_classify_wake_called_exactly_once_per_run_wake(cfg, monkeypatch):
    """Fix 1 core invariant: run_wake calls _classify_wake EXACTLY ONCE per wake
    (never a second classification/peek pass outside the lock) -- the prior
    two-read design (classify, then a second later peek_rotated() for
    rotate_claim) is what let a rotate loser observe a different answer than its
    own classification."""
    from cortex import note, symlinks, wake, watchdog, window

    wake_state.set_rotated(cfg)
    _seed_wake_row(cfg, "single-classify")

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(note, "seed_baseline", lambda *a, **k: None)
    monkeypatch.setattr(wake, "_render_daybrief", lambda c: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")

    calls = {"n": 0}
    real_classify = wake._classify_wake

    def _spy(c):
        calls["n"] += 1
        return real_classify(c)
    monkeypatch.setattr(wake, "_classify_wake", _spy)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc),
                      caller=wake.call_marrow_cortex)
    finally:
        conn.close()
    assert calls["n"] == 1


# ── Fix 2: readiness returns verified or raises ───────────────────────────────

def test_wait_ready_raises_on_timeout(cfg, monkeypatch):
    """Fix 2: _wait_ready must RAISE WindowError when the readiness marker never
    appears (a bad/gone --resume sid or an instantly-exiting claude leaves a bare
    shell) -- it previously returned identically on found and on timeout, so a
    dead resume was recorded as an awake resident."""
    from cortex import window

    cfg["wake"]["ready_timeout_sec"] = 0.01
    monkeypatch.setattr(window, "_read_session", lambda sid: "bare shell, no marker")
    with pytest.raises(window.WindowError):
        window._wait_ready("SID-X", cfg)


def test_wait_ready_returns_when_marker_present(cfg, monkeypatch):
    """Companion: the marker present -> returns cleanly (no raise)."""
    from cortex import window

    cfg["wake"]["ready_timeout_sec"] = 1
    monkeypatch.setattr(window, "_read_session",
                        lambda sid: "footer ... accept edits ... ready")
    window._wait_ready("SID-Y", cfg)  # no exception


def test_respawn_readiness_timeout_does_not_persist_sid(cfg, monkeypatch):
    """Fix 2: a resume whose TUI never comes up must NOT record the bare shell as
    the resident session. respawn raises WindowError (from _wait_ready) before
    set_session_id, so no stale sid is left behind."""
    from cortex import window

    cfg["wake"]["ready_timeout_sec"] = 0.01
    monkeypatch.setattr(window, "_spawn",
                        lambda c, initial_prompt=None, resume_sid=None: "BARE-SID")
    monkeypatch.setattr(window, "_read_session", lambda sid: "no marker here")

    with pytest.raises(window.WindowError):
        window.respawn(cfg, resume_sid="gone-uuid")
    assert wake_state.get_session_id(cfg) is None  # bare shell never recorded


def test_spawn_wake_resume_readiness_failure_surfaces_none(cfg, monkeypatch):
    """Fix 2 end-to-end: a resume whose respawn raises (readiness timeout) makes
    _spawn_wake return None, which _resume_or_fresh_dead turns into a fresh-with-
    catchup retry -- the documented fresh fallback finally fires."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-timeout")
    wake_state.update(cfg, transcript="/x/projects/cwd/live-uuid.jsonl")

    calls = []

    def _respawn(c, initial_prompt=None, resume_sid=None):
        calls.append(resume_sid)
        if resume_sid:
            raise window.WindowError("resumed TUI never became ready")
        return "fresh-iterm-sid"
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)  # dead resident
    monkeypatch.setattr(window, "respawn", _respawn)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    conn = db.connect(cfg)
    try:
        res = wake._window_wake(conn, cfg, "N", datetime.now(timezone.utc))
    finally:
        conn.close()
    assert res is not None and res["mode"] == "window"
    assert calls == ["live-uuid", None]  # resume tried, then fresh fallback fired
    note_text = wake_state.wakeup_note_path(cfg).read_text()
    assert "died without a handoff" in note_text  # fresh fallback -> catchup


# ── Fix 3: resumed wake -> conditional machine-tagged bell ────────────────────

def _write_assistant_lines(cfg, resume_sid: str, n: int) -> None:
    from cortex import transcript

    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    rows = [{"type": "assistant", "message": {"role": "assistant"}} for _ in range(n)]
    (tdir / f"{resume_sid}.jsonl").write_text("\n".join(json.dumps(r) for r in rows))


def test_resume_fallback_types_bell_when_no_model_turn(cfg, monkeypatch):
    """Fix 3: the resumed conversation produces NO new assistant line within the
    timeout (the prior session had no armed Monitor for the harness to report),
    so cortex types ONE machine-tagged bell (via type_wake_signal) carrying the
    epoch token in its receipt. baseline is the PRE-LAUNCH count (passed in
    explicitly, never re-captured inside the function -- codex adversarial-review
    Fix 3)."""
    from cortex import wake, window

    cfg["wake"]["resume_turn_timeout_sec"] = 0.02
    _write_assistant_lines(cfg, "resume-uuid", 1)  # never grows past baseline

    typed = {}
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: typed.setdefault("token", token) or True)
    monkeypatch.setattr(wake.time, "sleep", lambda s: None)

    token = (5, "cafe")
    wake._resume_fallback_bell(cfg, datetime.now(timezone.utc),
                               token, "resume-uuid", baseline=1)
    assert typed["token"] == token  # bell typed with the epoch token


def test_resume_fallback_stays_silent_when_model_turn_appears(cfg, monkeypatch):
    """Fix 3: a NEW assistant line appears (the harness's own background-shell
    notice drove a model turn) -> no bell, no receipt. Detection is on a NEW
    assistant-role LINE, not mtime (a hook write touches mtime -> false positive
    observed live)."""
    from cortex import wake, window

    cfg["wake"]["resume_turn_timeout_sec"] = 1.0
    _write_assistant_lines(cfg, "resume-uuid", 1)  # baseline = 1

    typed = {"called": False}
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: typed.__setitem__("called", True))

    # After the first poll sleep, a new assistant line appears (count -> 2).
    real_sleep = wake.time.sleep
    state = {"grown": False}

    def _sleep(s):
        if not state["grown"]:
            state["grown"] = True
            _write_assistant_lines(cfg, "resume-uuid", 2)  # model turn landed

    monkeypatch.setattr(wake.time, "sleep", _sleep)

    wake._resume_fallback_bell(cfg, datetime.now(timezone.utc),
                               (5, "cafe"), "resume-uuid", baseline=1)
    assert typed["called"] is False  # model turn seen -> silent, no bell


def test_assistant_line_count_ignores_non_assistant(cfg):
    """The counter only counts top-level type=='assistant' entries (user turns,
    tool results, blank lines are ignored)."""
    from cortex import wake

    from cortex import transcript
    tdir = transcript.transcript_dir(cfg)
    tdir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "message": {"role": "user"}},
        {"type": "assistant", "message": {"role": "assistant"}},
        {"type": "user", "message": {"role": "user"}},
        {"type": "assistant", "message": {"role": "assistant"}},
    ]
    (tdir / "sid.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot-json\n")
    assert wake._assistant_line_count(cfg, "sid") == 2
    assert wake._assistant_line_count(cfg, "missing") == 0


def test_resume_launch_is_clean_no_receipt(cfg, monkeypatch):
    """Fix 3: the resume LAUNCH itself types no bell and writes no receipt -- only
    the fallback (when it fires) does. Here the fallback is stubbed out; assert
    the launch was clean (initial_prompt None, no receipt written at launch)."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "resume-clean")
    wake_state.update(cfg, transcript="/x/projects/cwd/live-uuid.jsonl")

    launch = {}
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None:
                        launch.update(prompt=initial_prompt, resume_sid=resume_sid))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(wake, "_resume_fallback_bell", lambda *a, **k: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    conn = db.connect(cfg)
    try:
        wake._window_wake(conn, cfg, "N", datetime.now(timezone.utc))
    finally:
        conn.close()
    assert launch["resume_sid"] == "live-uuid"
    assert launch["prompt"] is None                    # clean launch, no bell baked
    assert "wake_receipt" not in wake_state.load(cfg)  # no receipt at launch


# ── Fix 4: epoch cancellation on the slow fresh/resume spawn ──────────────────

def test_fresh_spawn_receipt_carries_epoch_token(cfg, monkeypatch):
    """Fix 4: the fresh receipt carries the captured (gen, state_id); set_awake
    uses bump=False so the LIVE gen still equals the receipt gen (a bump would
    make the marrow hook read the receipt as stale and suppress the note)."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "fresh-token")
    monkeypatch.setattr(window, "respawn",
                        lambda c, initial_prompt=None, resume_sid=None: "sid-new")
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    conn = db.connect(cfg)
    try:
        wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=False)
    finally:
        conn.close()

    d = wake_state.load(cfg)
    r = d["wake_receipt"]
    assert isinstance(r["gen"], int)
    assert r["state_id"] == d["state_id"]
    assert d["gen"] == r["gen"]   # live gen == receipt gen (not bumped)
    assert d["awake"] is True
    # Fix 2: the new session id is committed atomically with the awake flip
    # (set_awake's session_id= param) -- window.respawn itself never persists it.
    assert d["session_id"] == "sid-new"


# ── Fix 5: machine-origin tag on the wake note ────────────────────────────────

def test_note_render_prepends_machine_tag(cfg):
    """Fix 5: the rendered wake note opens with the config-driven machine tag so
    the model treats the delivering ☀️ turn as an automated scheduler signal,
    not user speech."""
    from cortex import note

    conn = db.connect(cfg)
    try:
        now = datetime.now(timezone.utc)
        text = note.render(cfg, now, note.gather(conn, cfg, now))
    finally:
        conn.close()
    tag = cfg["note"]["wake_machine_tag"]
    assert tag  # default is non-empty
    assert text.startswith(tag)


def test_note_render_machine_tag_config_toggle(cfg):
    """Fix 5 config-first: blanking wake_machine_tag omits the line entirely; the
    tag is never hardcoded in .py."""
    from cortex import note

    cfg["note"]["wake_machine_tag"] = ""
    conn = db.connect(cfg)
    try:
        now = datetime.now(timezone.utc)
        text = note.render(cfg, now, note.gather(conn, cfg, now))
    finally:
        conn.close()
    assert not text.startswith("[AUTOMATED WAKE SIGNAL")


# ===========================================================================
# codex adversarial-review, round 2: deterministic interleaving tests for the
# four high findings against c553d52 (rotate sampling gap / stale sid
# persistence / pre-readiness assistant turn / state_id ABA). Each test drives
# the exact interleave the finding describes, not just the end-state assertion.
# ===========================================================================

def test_interleave_rotate_sampling_gap_no_double_spawn(cfg, monkeypatch):
    """Finding 1 interleave: classification must be ONE lock-protected read, not
    two (plan, then a LATER separate peek_rotated() for rotate_claim). Drives the
    exact gap: entrant A's _classify_wake call reports rotate_driven=True: a
    concurrent winner (simulated) then consumes the flag BEFORE A's belt-and-
    braces re-check -- A must see the flag gone and skip, never double-spawning.
    (The single-classify structure makes this gap structurally unreachable in
    production; this test exercises the surviving guard directly.)"""
    from cortex import note, symlinks, wake, watchdog, window

    _seed_wake_row(cfg, "interleave-rotate")
    wake_state.set_rotated(cfg)

    monkeypatch.setattr(symlinks, "ensure_all", lambda c: None)
    monkeypatch.setattr(note, "seed_baseline", lambda *a, **k: None)
    monkeypatch.setattr(wake, "_render_daybrief", lambda c: None)
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)

    spawns = {"n": 0}
    monkeypatch.setattr(
        window, "respawn",
        lambda c, initial_prompt=None, resume_sid=None: spawns.__setitem__("n", spawns["n"] + 1))
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")

    # _classify_wake runs for real (sees the real flag -> ("fresh", True)), but a
    # concurrent winner (modelled directly) consumes the flag the INSTANT after
    # classification returns, before this entrant's in-lock re-peek runs.
    real_classify = wake._classify_wake

    def _classify_then_race(c):
        result = real_classify(c)
        wake_state.take_rotated(c)  # the "winner" consumes it right here
        return result
    monkeypatch.setattr(wake, "_classify_wake", _classify_then_race)

    decision = {"wake": True, "reasons": [], "wake_reasons": "ctl"}
    conn = db.connect(cfg)
    try:
        res = wake.run_wake(conn, cfg, decision, now=datetime.now(timezone.utc),
                            caller=wake.call_marrow_cortex)
    finally:
        conn.close()

    assert spawns["n"] == 0                       # never double-spawned
    assert res.get("skipped") == "spawn_race_lost"


def test_interleave_stale_sid_never_overwrites_live_resident(cfg, monkeypatch):
    """Finding 2 interleave: the epoch advances WHILE the window is booting
    (between _wait_ready succeeding and the caller's set_awake CAS). Drives the
    exact ordering: respawn() returns a verified sid WITHOUT persisting it; a
    'concurrent' actor (simulated inside the respawn stub, i.e. strictly between
    epoch capture and the CAS) commits ITS OWN new resident sid and bumps gen;
    the original (now-stale) spawn's CAS must reject and its sid must never reach
    wake_state -- the live resident stays the concurrent actor's sid throughout."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "interleave-sid")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")

    def _respawn_then_concurrent_actor_wins(c, initial_prompt=None, resume_sid=None):
        # Interleave point: THIS stale actor's window just became ready (respawn
        # about to return its verified sid) -- but before it can commit, a
        # concurrent actor (e.g. a user reset spawning its own fresh window) wins
        # the race: it commits its own session id + bumps the epoch first.
        wake_state.set_awake(cfg, None, "/concurrent/winner.jsonl",
                             session_id="concurrent-winner-sid")
        return "stale-actor-sid"  # verified-ready, but never committed
    monkeypatch.setattr(window, "respawn", _respawn_then_concurrent_actor_wins)

    token = wake_state.current_epoch(cfg)  # captured BEFORE the interleaved respawn
    conn = db.connect(cfg)
    try:
        # Directly exercise the CAS ordering _spawn_wake relies on: capture token
        # (already done above), respawn (interleaves the concurrent winner in),
        # then the conditional commit with the STALE token.
        new_sid = window.respawn(cfg, resume_sid=None)
        new_epoch = wake_state.set_awake(
            cfg, None, "/stale/actor.jsonl", expected_token=token, bump=False,
            session_id=new_sid)
    finally:
        conn.close()

    assert new_epoch is None  # the stale actor's CAS rejected
    d = wake_state.load(cfg)
    # The live resident is STILL the concurrent winner's -- the stale actor's sid
    # ("stale-actor-sid") never touched wake_state at all.
    assert d["session_id"] == "concurrent-winner-sid"
    assert d["transcript"] == "/concurrent/winner.jsonl"


def test_interleave_pre_readiness_assistant_turn_counts_as_growth(cfg, monkeypatch):
    """Finding 3 interleave: a harness-driven assistant turn is written DURING
    the `claude --resume` launch/readiness window (a real, multi-second window),
    i.e. strictly BETWEEN the pre-launch baseline capture and respawn() returning
    -- not after. The baseline must already be captured before this turn lands,
    so the fallback-bell poll sees it as growth immediately and never fires."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "interleave-preready")
    wake_state.update(cfg, transcript="/x/projects/cwd/resume-uuid.jsonl")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")
    monkeypatch.setattr(window, "claude_session_id", lambda c: "resume-uuid")

    typed = {"called": False}
    monkeypatch.setattr(window, "type_wake_signal",
                        lambda c, now, token=None: typed.__setitem__("called", True))

    def _respawn_writes_turn_during_readiness(c, initial_prompt=None, resume_sid=None):
        # The interleave: a harness-driven assistant turn lands WHILE the window
        # is still coming up -- i.e. after _spawn_wake captured resume_baseline
        # but before respawn() (which wraps _wait_ready) returns.
        _write_assistant_lines(cfg, "resume-uuid", 1)
        return "resumed-iterm-sid"
    monkeypatch.setattr(window, "respawn", _respawn_writes_turn_during_readiness)

    conn = db.connect(cfg)
    try:
        res = wake._spawn_wake(conn, cfg, datetime.now(timezone.utc), resume=True)
    finally:
        conn.close()

    assert res is not None and res["mode"] == "window"
    assert typed["called"] is False  # growth already present -> no bell fired
    assert wake_state.load(cfg)["awake"] is True  # awake flip still committed


def test_interleave_state_id_aba_rejects_recreated_state(cfg, monkeypatch):
    """Finding 4 interleave: wake_state.json is DELETED and RECREATED (e.g. a
    corrupt-state repair, or a wipe) landing back on the SAME gen but a NEW
    state_id, strictly BETWEEN the token capture and the CAS. A gen-only compare
    would pass (ABA) and let the stale actor overwrite the recreated state; the
    FULL (gen, state_id) token must reject it."""
    from cortex import wake, watchdog, window

    _seed_wake_row(cfg, "interleave-aba")
    monkeypatch.setattr(watchdog, "spawn", lambda c: None)
    monkeypatch.setattr(wake, "_wait_new_transcript", lambda c, prev, ts: "/t/new.jsonl")

    token = wake_state.current_epoch(cfg)  # (gen, original_state_id)

    def _respawn_recreates_state_same_gen(c, initial_prompt=None, resume_sid=None):
        # Interleave: delete + recreate the state file BETWEEN token capture and
        # the CAS, landing back on the SAME gen with a DIFFERENT state_id (the
        # ABA wake_state.json's _ensure_epoch re-seeds on first touch after a
        # wipe -- a fresh random state_id, same starting gen 0/whatever it was).
        wake_state.wake_state_path(cfg).unlink(missing_ok=True)
        new_gen, new_state_id = wake_state.current_epoch(cfg)  # re-seeds the file
        assert new_gen == token[0]              # same gen (the ABA condition)
        assert new_state_id != token[1]          # different identity
        return "stale-sid"
    monkeypatch.setattr(window, "respawn", _respawn_recreates_state_same_gen)

    conn = db.connect(cfg)
    try:
        new_sid = window.respawn(cfg, resume_sid=None)
        # The stale actor's CAS uses the ORIGINAL token (captured before the
        # interleaved delete/recreate) -- gen matches the recreated file's gen,
        # but state_id does not.
        new_epoch = wake_state.set_awake(
            cfg, None, "/stale/actor.jsonl", expected_token=token, bump=False,
            session_id=new_sid)
    finally:
        conn.close()

    assert new_epoch is None  # rejected despite the gen-only match (ABA closed)
    d = wake_state.load(cfg)
    assert d.get("session_id") != "stale-sid"   # stale actor never committed
    assert d.get("awake") is not True
