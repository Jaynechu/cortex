from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import wake

TZ = timezone(timedelta(hours=10))
DAY1 = datetime(2026, 7, 3, 21, 0, tzinfo=TZ)
DAY2 = datetime(2026, 7, 4, 9, 0, tzinfo=TZ)

DECISION = {"wake": True, "reasons": [], "gated_by": [], "explanation": "test wake"}


@pytest.fixture(autouse=True)
def events_table(marrow_conn):
    marrow_conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, session_id TEXT, timestamp TEXT, "
        "role TEXT, content TEXT, ts_start TEXT, ts_end TEXT)"
    )
    marrow_conn.commit()


@pytest.fixture(autouse=True)
def stub_daybrief(monkeypatch):
    """daybrief render shells out to marrow's venv (unavailable in tests) —
    stub it so wake tests exercise only the wake logic."""
    monkeypatch.setattr(wake, "_render_daybrief", lambda cfg: None)


@pytest.fixture
def wcfg(base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = {
        **base_cfg["paths"],
        "cortex_home": str(tmp_path / "cortex_home"),
        "wishlist_file": str(tmp_path / "cortex_home" / "wishlist.md"),
        "ny_db_pages": str(tmp_path / "ny"),
        "wake_timing_log": str(tmp_path / "wake_timing.log"),
    }
    cfg["marrow"] = {"repo_dir": "", "venv_python": "", "call_timeout_s": 5}
    cfg["wake"] = {"token_cap": 150_000}
    return cfg


class FakeCaller:
    def __init__(self, session_id="sid-abc"):
        self.session_id = session_id
        self.calls = []

    def __call__(self, prompt, cwd, resume_sid, cfg):
        self.calls.append({"prompt": prompt, "cwd": cwd, "resume_sid": resume_sid})
        return {"text": "hi", "session_id": self.session_id}


def test_assemble_note_real_data(marrow_conn, wcfg):
    text = wake.assemble_note(marrow_conn, wcfg, DAY1)
    assert text.startswith("Now:")  # note leads with Now; Wake reason line retired
    assert "Wake:" not in text
    assert len(text) < 1000


def test_first_wake_no_resume_and_persists_session(marrow_conn, wcfg):
    caller = FakeCaller()
    result = wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)

    assert result["session_id"] == "sid-abc"
    assert caller.calls[0]["resume_sid"] is None
    assert caller.calls[0]["cwd"] == str(wcfg["paths"]["cortex_home"])

    from cortex.pacemaker import integration
    state = integration.load_state(marrow_conn)
    assert state.cortex_session_id == "sid-abc"


def test_second_wake_same_day_resumes(marrow_conn, wcfg):
    caller = FakeCaller()
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)
    later = DAY1 + timedelta(hours=1)
    wake.run_wake(marrow_conn, wcfg, DECISION, now=later, caller=caller)

    assert len(caller.calls) == 2
    assert caller.calls[1]["resume_sid"] == "sid-abc"


def test_new_date_resumes_no_rebirth(marrow_conn, wcfg):
    """Rebirth retired: a new local date no longer starts a fresh session or
    archives. The headless path resumes the prior session as any same-day wake
    would; freshness now comes only from the rotate/night-close path."""
    caller = FakeCaller(session_id="sid-day1")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)

    caller2 = FakeCaller(session_id="sid-day2")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY2, caller=caller2)

    assert caller2.calls[0]["resume_sid"] == "sid-day1"  # resumed, not reborn

    from cortex.pacemaker import integration
    state = integration.load_state(marrow_conn)
    assert state.cortex_session_id == "sid-day2"


def test_run_wake_creates_ny_symlinks(marrow_conn, wcfg):
    caller = FakeCaller()
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)

    from pathlib import Path
    ny = Path(wcfg["paths"]["ny_db_pages"])
    assert (ny / "wishlist.md").is_symlink()
    assert (ny / "wishlist.md").resolve() == Path(wcfg["paths"]["wishlist_file"]).resolve()


class FailCaller:
    def __call__(self, prompt, cwd, resume_sid, cfg):
        raise wake.WakeError("boom")


def test_failed_wake_forces_fresh_next_no_archive(marrow_conn, wcfg):
    """A failed marrow call drops the resume sid (fresh session next wake).
    Rebirth/archiving is retired -> no archive dir is created."""
    from cortex.pacemaker import integration

    good = FakeCaller(session_id="sid-day1")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=good)

    with pytest.raises(wake.WakeError):
        wake.run_wake(marrow_conn, wcfg, DECISION,
                      now=DAY1 + timedelta(hours=1), caller=FailCaller())

    st = integration.load_state(marrow_conn)
    assert st.cortex_session_id is None            # fresh session next wake

    # Retry resumes fresh (resume None) and persists the new sid.
    good2 = FakeCaller(session_id="sid-retry")
    wake.run_wake(marrow_conn, wcfg, DECISION,
                  now=DAY1 + timedelta(hours=2), caller=good2)
    assert good2.calls[0]["resume_sid"] is None
    assert integration.load_state(marrow_conn).cortex_session_id == "sid-retry"


def test_call_marrow_cortex_outer_timeout_derives_from_inner(monkeypatch, wcfg):
    """Outer subprocess kill = inner budget + margin; inner budget is passed
    down to marrow so the two layers share one config value."""
    cfg = dict(wcfg)
    cfg["marrow"] = {**wcfg["marrow"], "call_timeout_s": 100,
                     "repo_dir": "/repo", "venv_python": "/py"}
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["timeout"] = kw["timeout"]
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(wake.subprocess, "run", fake_run)
    with pytest.raises(wake.WakeError, match="130s"):
        wake.call_marrow_cortex("prompt", "/cwd", None, cfg)

    assert captured["timeout"] == 130
    assert captured["cmd"][-2] == "100"  # inner budget handed to marrow script
    assert captured["cmd"][-1] == "150000"  # per-wake token cap handed down


class CapCaller:
    """Simulates a marrow wake that tripped the per-wake token cap mid-stream."""
    def __init__(self):
        self.calls = []

    def __call__(self, prompt, cwd, resume_sid, cfg):
        self.calls.append({"resume_sid": resume_sid})
        return {"text": "", "session_id": None, "capped": True,
                "total_tokens": 160000}


def test_token_cap_breach_forces_fresh_no_rearchive(marrow_conn, wcfg):
    """A mid-wake token-cap breach drops the resume sid (fresh session next
    wake). Rebirth/archiving is retired -> the same day's log is never
    re-archived."""
    from cortex.pacemaker import integration

    good = FakeCaller(session_id="sid-1")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=good)

    later = DAY1 + timedelta(hours=1)
    cap = CapCaller()
    res = wake.run_wake(marrow_conn, wcfg, DECISION, now=later, caller=cap)
    assert res["capped"] is True
    assert cap.calls[0]["resume_sid"] == "sid-1"  # resumed before the breach

    st = integration.load_state(marrow_conn)
    assert st.cortex_session_id is None            # fresh session next wake

    # Third wake same day: fresh (resume None), still no archive of day1.
    good2 = FakeCaller(session_id="sid-3")
    wake.run_wake(marrow_conn, wcfg, DECISION,
                  now=later + timedelta(hours=1), caller=good2)
    assert good2.calls[0]["resume_sid"] is None
    assert integration.load_state(marrow_conn).cortex_session_id == "sid-3"


# --------------------------------------------------------------------------- #
# Rotate (handoff round-trip): a rotated/respawned resident window is a fresh
# brain and must receive the previous brain's handoff note.
# --------------------------------------------------------------------------- #

@pytest.fixture
def rot_cfg(wcfg, tmp_path):
    """wcfg + handoff note config + a written handoff file, mode=window."""
    cfg = dict(wcfg)
    cfg["wake"] = {**wcfg["wake"], "mode": "window"}
    cfg["paths"] = {**wcfg["paths"], "handoff_file": str(tmp_path / "handoff.md"),
                    "wake_state_file": str(tmp_path / "wake_state.json")}
    cfg["note"] = {"handoff_wake_kinds": ["rotate"],
                   "handoff_title": "handoff-note"}
    Path(cfg["paths"]["handoff_file"]).write_text("carry this to your next self")
    return cfg


def test_window_rotated_flag_path(monkeypatch, rot_cfg):
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.set_rotated(rot_cfg)
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: 4242)
    monkeypatch.setattr(transcript, "newest", lambda cfg: None)
    assert wake._window_rotated(rot_cfg) is True
    # flag consumed (read-and-clear): a second check without a new signal is False
    assert wake._window_rotated(rot_cfg) is False


def test_window_rotated_transcript_diff_path(monkeypatch, rot_cfg):
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.update(rot_cfg, transcript="/t/old.jsonl")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: 4242)
    monkeypatch.setattr(transcript, "newest", lambda cfg: Path("/t/new.jsonl"))
    assert wake._window_rotated(rot_cfg) is True


def test_window_rotated_dead_window_is_fresh(monkeypatch, rot_cfg):
    from cortex import wake_state, window
    wake_state.set_session_id(rot_cfg, "sid-1")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: False)
    assert wake._window_rotated(rot_cfg) is True


def test_window_rotated_claude_dead_is_fresh(monkeypatch, rot_cfg):
    """Session exists but its `claude` process died (SIGINT/crash) -> bare
    shell -> treated as fresh so ensure_window's relaunch gets the handoff."""
    from cortex import wake_state, window
    wake_state.set_session_id(rot_cfg, "sid-1")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: None)
    assert wake._window_rotated(rot_cfg) is True


def test_window_unrotated_resume_stays_non_fresh(monkeypatch, rot_cfg):
    """Plain wake into a live, un-rotated window: same transcript, no flag ->
    NOT fresh (no handoff; replay continuity lives in the window's own context)."""
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.update(rot_cfg, transcript="/t/same.jsonl")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(window, "find_claude_pid", lambda cfg: 4242)
    monkeypatch.setattr(transcript, "newest", lambda cfg: Path("/t/same.jsonl"))
    assert wake._window_rotated(rot_cfg) is False


def test_window_wake_rotate_respawns(monkeypatch, marrow_conn, rot_cfg):
    """Full window-branch: a rotated window (same local day) respawns fresh.
    The handoff now injects at SessionStart (marrow), not in the note."""
    monkeypatch.setattr(wake, "_window_wake_plan", lambda cfg: "fresh")
    captured = {}
    def fake_window_wake(conn, cfg, note_text, now, respawn=False, **kw):
        captured["text"] = note_text
        captured["respawn"] = respawn
        return {"mode": "window", "session_id": None, "text": None}
    monkeypatch.setattr(wake, "_window_wake", fake_window_wake)
    # same-day second wake (not rebirth): seed today's session date
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)  # first wake seeds state
    captured.clear()
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1 + timedelta(hours=1))
    assert "handoff-note" not in captured["text"]  # handoff moved to SessionStart
    assert captured["respawn"] is True  # rotate -> fresh self-arming window


def test_rotate_flag_makes_next_wake_fresh(monkeypatch, marrow_conn, rot_cfg):
    """Freshness comes only from the rotate path now (no rebirth): a set rotate
    flag makes the next window wake respawn a fresh brain with the handoff note.
    This is the mechanism the night close relies on for the first post-night wake."""
    from cortex import wake_state
    wake_state.set_rotated(rot_cfg)
    captured = {}
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now, respawn=False, **kw:
                        captured.update(text=t, respawn=respawn) or
                        {"mode": "window", "session_id": None, "text": None})
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)
    assert captured["respawn"] is True          # rotate flag -> fresh respawn


def test_window_wake_unrotated_no_handoff(monkeypatch, marrow_conn, rot_cfg):
    """Un-rotated same-day wake: no handoff in the note."""
    monkeypatch.setattr(wake, "_window_wake_plan", lambda cfg: "ear")
    captured = {}
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now, respawn=False, **kw:
                        captured.update(text=t, respawn=respawn) or
                        {"mode": "window", "session_id": None, "text": None})
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)
    captured.clear()
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1 + timedelta(hours=1))
    assert "handoff-note" not in captured["text"]
    assert captured["respawn"] is False         # live un-rotated window: no respawn


# --------------------------------------------------------------------------- #
# retired_sid: durable per-session rotate guard (belt-and-braces over the
# one-shot `rotated` flag / stale `transcript` pointer going out of sync).
# --------------------------------------------------------------------------- #

def test_window_wake_plan_clears_transcript_on_rotate_consume(rot_cfg):
    """The transcript pointer is cleared at the SAME moment take_rotated
    consumes the flag, so nothing in between can read it as still live."""
    from cortex import wake_state
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.update(rot_cfg, transcript="/t/retiring.jsonl")
    wake_state.set_rotated(rot_cfg)
    assert wake._window_wake_plan(rot_cfg) == "fresh"
    assert wake_state.load(rot_cfg).get("transcript") is None


def test_resume_or_fresh_dead_normal_resume(monkeypatch, marrow_conn, rot_cfg):
    """Baseline: an un-retired resumable sid resumes normally."""
    from cortex import transcript, wake_state
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    wake_state.update(rot_cfg, transcript="/t/alive-sid.jsonl")
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, cfg, now, resume=False, **kw:
                        captured.update(resume=resume) or {"mode": "window"})
    wake._resume_or_fresh_dead(marrow_conn, rot_cfg, DAY1, "test")
    assert captured["resume"] is True


def test_resume_or_fresh_dead_retired_sid_forces_fresh(monkeypatch, marrow_conn, rot_cfg):
    """Coordinator repro: `rotated` already consumed by an earlier wake, but
    the stale transcript pointer still resolves to the retired session's sid.
    retired_sid must block the resume and force a fresh spawn instead — this
    is the single choke point both ctl.cmd_wake's dead-branch and tick
    reconcile's resume share (_window_wake -> _resume_or_fresh_dead)."""
    from cortex import transcript, wake_state
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    # rotated already consumed elsewhere; stale pointer still names the
    # retired session (durably recorded via set_retired_sid at rotate time).
    wake_state.update(rot_cfg, transcript="/t/retired-sid.jsonl")
    wake_state.set_retired_sid(rot_cfg, "/t/retired-sid.jsonl")
    assert wake_state.load(rot_cfg).get("rotated") is None  # already consumed
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, cfg, now, resume=False, **kw:
                        captured.update(resume=resume) or {"mode": "window"})
    wake._resume_or_fresh_dead(marrow_conn, rot_cfg, DAY1, "test")
    assert captured["resume"] is False  # never resumes a retired session


def test_resume_or_fresh_dead_seeds_from_delivered_catchup_cutoff(
        monkeypatch, marrow_conn, rot_cfg):
    """#3: the dead-no-handoff branch REPLACES the first note with a second
    died_no_handoff catch-up note before spawning. Seeding must anchor to the
    DELIVERED (catch-up) note's cutoff — propagated via win['note_cutoff'] — not
    the first note's. An event arriving between the two assemblies is shown in
    the delivered note yet, if seeded from the first note's older cutoff, would
    stay > baseline and duplicate in the first free-round.

    Here we prove the replacement note's cutoff is captured and returned. The
    catch-up note is assembled inside _resume_or_fresh_dead; its cutoff reflects
    the racer event present at that assembly, and that exact cutoff rides back on
    the result dict for run_wake's seed_baseline call."""
    from cortex import transcript, wake_state, window
    # Rebuild events with a channel column (the autouse fixture omits it, which
    # the replay query needs to see the racer event).
    marrow_conn.execute("DROP TABLE events")
    marrow_conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "timestamp TEXT, role TEXT, content TEXT, channel TEXT)")
    marrow_conn.commit()

    # No resumable sid + no handoff -> the fresh-with-catchup branch.
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    monkeypatch.setattr(window, "claude_session_id", lambda cfg: None)
    monkeypatch.setattr(wake, "_handoff_written_this_window", lambda cfg: False)
    monkeypatch.setattr(window, "write_note", lambda cfg, text: None)
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, cfg, now, resume=False, **kw: {"mode": "window"})
    monkeypatch.setattr(wake.note, "_frontmost_app", lambda: None)

    # Event that races in AFTER the first note but is present when the catch-up
    # note is assembled inside _resume_or_fresh_dead.
    marrow_conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) VALUES (?,?,?,?,?)",
        ("s", "2026-07-08T03:00:30+00:00", "user", "raced between assemblies", "wx"))
    marrow_conn.commit()

    result = wake._resume_or_fresh_dead(marrow_conn, rot_cfg, DAY1, "test")
    # The delivered catch-up note's cutoff rides back for seeding.
    assert result["note_cutoff"] == "2026-07-08T03:00:30+00:00"

    # Seeding from the delivered cutoff: the racer is at the baseline, so it is
    # NOT re-shown (not duplicated) in the first free-round.
    wake_state.set_awake(rot_cfg, None, None)  # resets last_note_ts to None
    wake.note.seed_baseline(marrow_conn, rot_cfg, cutoff_ts=result["note_cutoff"])
    d = wake.note.gather(marrow_conn, rot_cfg, DAY1, advance_baseline=True)
    assert d["replay"] == []  # racer already delivered -> not duplicated


def test_resume_or_fresh_dead_no_note_replacement_keeps_first_cutoff(
        monkeypatch, marrow_conn, rot_cfg):
    """#3 counterpart: when the dead path does NOT replace the note (handoff was
    written this window), no note_cutoff key is set, so run_wake keeps the first
    note's captured cutoff."""
    from cortex import transcript, window
    monkeypatch.setattr(transcript, "newest_window_lineage", lambda cfg, marker: None)
    monkeypatch.setattr(window, "claude_session_id", lambda cfg: None)
    monkeypatch.setattr(wake, "_handoff_written_this_window", lambda cfg: True)
    captured = {}
    monkeypatch.setattr(wake, "_spawn_wake",
                        lambda conn, cfg, now, resume=False, **kw:
                        captured.update(spawned=True) or {"mode": "window"})
    result = wake._resume_or_fresh_dead(marrow_conn, rot_cfg, DAY1, "test")
    assert captured.get("spawned") is True
    assert "note_cutoff" not in result  # no replacement -> caller keeps first cutoff


def test_night_close_idle_rotate_records_retired_sid(monkeypatch, night_cfg):
    """_night_close's idle-resident rotate branch also records retired_sid,
    same as lie_down(rotate=True) — this is the exact path in the coordinator's
    repro (handoff written 23:01 via night close, not an explicit ctl sleep)."""
    from cortex import pacemaker_tick, wake_state, window
    wake_state.set_session_id(night_cfg, "sid-1")
    wake_state.update(night_cfg, transcript="/t/b1a7aae2.jsonl")
    monkeypatch.setattr(window, "inject_prompt", lambda cfg, text: True)
    st = wake_state.load(night_cfg)  # no awake key -> already lying down
    pacemaker_tick._night_close(night_cfg, NIGHT, st)
    assert wake_state.load(night_cfg).get("rotated") is True
    assert wake_state.get_retired_sid(night_cfg) == "b1a7aae2"


# --------------------------------------------------------------------------- #
# Night close (replaces rebirth): the 23:00 gate hands a still-awake resident
# window a wrap-up instruction, then marks the idle session non-resumable so the
# first post-night wake is a plain fresh spawn.
# --------------------------------------------------------------------------- #

NIGHT = datetime(2026, 7, 3, 23, 30, tzinfo=TZ)   # inside 23:00-06:00 window
DAYTIME = datetime(2026, 7, 3, 14, 0, tzinfo=TZ)  # outside night window


@pytest.fixture
def night_cfg(rot_cfg):
    cfg = dict(rot_cfg)
    cfg["gates"] = {"night": {"start": "23:00", "end": "06:00", "cap": 0,
                              "close_prompt": "wrap up now"}}
    return cfg


def test_night_close_awake_injects_wrapup_once(monkeypatch, night_cfg):
    from cortex import pacemaker_tick, wake_state, window
    wake_state.set_session_id(night_cfg, "sid-1")
    injected = []
    monkeypatch.setattr(window, "inject_prompt",
                        lambda cfg, text: injected.append(text) or True)

    st = wake_state.load(night_cfg)
    st["awake"] = True
    msg = pacemaker_tick._night_close(night_cfg, NIGHT, st)
    assert injected == ["wrap up now"]
    assert "wrap-up injected" in msg
    # awake still + same night -> no second injection (once-per-night guard)
    st2 = wake_state.load(night_cfg)
    st2["awake"] = True
    assert pacemaker_tick._night_close(night_cfg, NIGHT, st2) is None
    assert injected == ["wrap up now"]
    # rotate is NOT set while it is still awake (marked only once it lies down)
    assert wake_state.load(night_cfg).get("rotated") is None


def test_night_close_already_down_marks_rotated_only(monkeypatch, night_cfg):
    from cortex import pacemaker_tick, wake_state, window
    wake_state.set_session_id(night_cfg, "sid-1")
    monkeypatch.setattr(window, "inject_prompt",
                        lambda cfg, text: (_ for _ in ()).throw(
                            AssertionError("must not inject when already down")))

    st = wake_state.load(night_cfg)  # no awake key -> lying down
    msg = pacemaker_tick._night_close(night_cfg, NIGHT, st)
    assert wake_state.load(night_cfg).get("rotated") is True
    assert "non-resumable" in msg
    # once per night: a second tick same night is a no-op
    assert pacemaker_tick._night_close(night_cfg, NIGHT, wake_state.load(night_cfg)) is None


def test_night_close_awake_then_down_marks_rotated(monkeypatch, night_cfg):
    """Awake at 23:00 -> wrap-up injected; after it lies down, the next tick in
    the same night marks the session non-resumable (fresh spawn next wake)."""
    from cortex import pacemaker_tick, wake_state, window
    wake_state.set_session_id(night_cfg, "sid-1")
    monkeypatch.setattr(window, "inject_prompt", lambda cfg, text: True)

    st = wake_state.load(night_cfg)
    st["awake"] = True
    pacemaker_tick._night_close(night_cfg, NIGHT, st)          # injects
    assert wake_state.load(night_cfg).get("rotated") is None
    # it lied down -> awake cleared; next tick marks rotated
    pacemaker_tick._night_close(night_cfg, NIGHT, wake_state.load(night_cfg))
    assert wake_state.load(night_cfg).get("rotated") is True


def test_night_close_outside_window_noop(monkeypatch, night_cfg):
    from cortex import pacemaker_tick, wake_state, window
    wake_state.set_session_id(night_cfg, "sid-1")
    monkeypatch.setattr(window, "inject_prompt", lambda cfg, text: True)
    st = wake_state.load(night_cfg)
    assert pacemaker_tick._night_close(night_cfg, DAYTIME, st) is None
    assert wake_state.load(night_cfg).get("rotated") is None


def test_night_close_no_session_no_rotate(night_cfg):
    """No resident session -> nothing to retire; do not set the rotate flag."""
    from cortex import pacemaker_tick, wake_state
    st = wake_state.load(night_cfg)  # not awake, no session id
    assert pacemaker_tick._night_close(night_cfg, NIGHT, st) is None
    assert wake_state.load(night_cfg).get("rotated") is None


# --------------------------------------------------------------------------- #
# BUG A: every set_awake path binds a wake=1 row so "Last wake" counts it
# --------------------------------------------------------------------------- #

def _wake_rows(conn):
    return conn.execute(
        "SELECT id, reasons, force_slept FROM ct_wake_log WHERE wake=1 "
        "ORDER BY id").fetchall()


def test_log_activation_wake_row_writes_tagged_row(marrow_conn):
    """A non-tick wake logs its OWN wake=1 row, tagged, force_slept NULL (so
    force_slept-based auto-rate stats stay unaffected)."""
    from cortex.pacemaker import integration
    wid = integration.log_activation_wake_row(marrow_conn, DAY1, "user")
    assert isinstance(wid, int)
    rows = _wake_rows(marrow_conn)
    assert len(rows) == 1
    assert rows[0]["id"] == wid
    assert rows[0]["reasons"] == "user"
    assert rows[0]["force_slept"] is None


def test_wake_log_id_writes_fresh_row_for_non_tick_wake(marrow_conn):
    """Chokepoint: a tagged (user/ctl/reconcile/rotate) wake gets a FRESH row
    even when an older scheduled row exists — so 'Last wake' never reuses a
    stale noon row (the BUG A symptom)."""
    from cortex.pacemaker import integration
    # A stale scheduled row hours ago (the noon row in the incident).
    old_ts = (DAY1 - timedelta(minutes=280)).astimezone(timezone.utc).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons) VALUES (?, 1, 0, 'floor')",
        (old_ts,))
    marrow_conn.commit()
    old_id = _wake_rows(marrow_conn)[0]["id"]

    wid = wake._wake_log_id(marrow_conn, DAY1, "user")
    assert wid != old_id  # a new row, not the stale one
    rows = _wake_rows(marrow_conn)
    assert len(rows) == 2
    assert rows[-1]["reasons"] == "user"


def test_wake_log_id_reuses_latest_for_scheduled(marrow_conn):
    """Scheduled wake (wake_reasons=None): reuse the decision row run_tick already
    wrote — no duplicate activation row."""
    ts = DAY1.astimezone(timezone.utc).isoformat()
    marrow_conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, reasons) VALUES (?, 1, 0, 'floor')",
        (ts,))
    marrow_conn.commit()
    scheduled_id = _wake_rows(marrow_conn)[0]["id"]

    wid = wake._wake_log_id(marrow_conn, DAY1, None)
    assert wid == scheduled_id  # reused, not a new row
    assert len(_wake_rows(marrow_conn)) == 1


def test_main_print_note_no_marrow_call(monkeypatch, marrow_conn, wcfg, capsys):
    monkeypatch.setattr(wake.config, "load", lambda: wcfg)
    monkeypatch.setattr(wake.db, "connect", lambda cfg: marrow_conn)

    rc = wake.main(["--print-note"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Now:" in out


def test_main_force_wake_tags_ctl_reasons(monkeypatch, marrow_conn, wcfg):
    """Codex P2: `python -m cortex.wake --force` must carry a non-tick
    wake_reasons tag (like ctl/reconcile), or a manual force-wake reuses the
    latest old scheduled row exactly like the BUG A symptom this patch fixed."""
    monkeypatch.setattr(wake.config, "load", lambda: wcfg)
    monkeypatch.setattr(wake.db, "connect", lambda cfg: marrow_conn)
    captured = {}
    monkeypatch.setattr(
        wake, "run_wake",
        lambda conn, cfg, decision, now=None: captured.update(decision=decision))

    rc = wake.main(["--force"])

    assert rc == 0
    assert captured["decision"]["wake_reasons"] == "ctl"


def test_headless_wake_with_reasons_logs_activation_row(marrow_conn, wcfg):
    """Codex P2: a non-tick decision (wake_reasons set) that completes via the
    headless/marrow-subprocess path (true headless mode, or window-path-failed
    fallback) must still log its own tagged activation row — this path bypasses
    wake_state.set_awake entirely, so the ear/spawn chokepoint never runs."""
    caller = FakeCaller()
    tagged = {**DECISION, "wake_reasons": "ctl"}
    wake.run_wake(marrow_conn, wcfg, tagged, now=DAY1, caller=caller)

    rows = marrow_conn.execute(
        "SELECT reasons FROM ct_wake_log WHERE wake=1").fetchall()
    assert [r["reasons"] for r in rows] == ["ctl"]


def test_headless_wake_scheduled_no_reasons_writes_no_row(marrow_conn, wcfg):
    """Counterpart: a pacemaker-decided wake (wake_reasons absent/None) reuses
    run_tick's own decision row -> the headless path must not write a second
    one (no duplicate wake=1 rows for a scheduled wake)."""
    caller = FakeCaller()
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)

    n = marrow_conn.execute(
        "SELECT COUNT(*) AS n FROM ct_wake_log WHERE wake=1").fetchone()["n"]
    assert n == 0
