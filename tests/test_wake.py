from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cortex import day_log, wake

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


@pytest.fixture
def wcfg(base_cfg, tmp_path):
    cfg = dict(base_cfg)
    cfg["paths"] = {
        **base_cfg["paths"],
        "day_log": str(tmp_path / "day_log.md"),
        "day_log_archive_dir": str(tmp_path / "archive"),
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


def test_assemble_bulletin_real_data(marrow_conn, wcfg):
    text = wake.assemble_bulletin(marrow_conn, wcfg, DAY1)
    assert "Now:" in text
    assert "Wake:" in text  # new note format leads with the Wake (trigger) line
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
    assert state.cortex_session_date == "2026-07-03"

    path = wcfg["paths"]["day_log"]
    text = open(path).read()
    assert text.splitlines()[0] == "2026-07-03"
    assert day_log.STATUS_START in text


def test_second_wake_same_day_resumes(marrow_conn, wcfg):
    caller = FakeCaller()
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)
    later = DAY1 + timedelta(hours=1)
    wake.run_wake(marrow_conn, wcfg, DECISION, now=later, caller=caller)

    assert len(caller.calls) == 2
    assert caller.calls[1]["resume_sid"] == "sid-abc"


def test_rebirth_on_new_date_archives_and_resets_resume(marrow_conn, wcfg):
    caller = FakeCaller(session_id="sid-day1")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)

    caller2 = FakeCaller(session_id="sid-day2")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY2, caller=caller2)

    assert caller2.calls[0]["resume_sid"] is None

    from pathlib import Path
    archive_dir = Path(wcfg["paths"]["day_log_archive_dir"])
    assert (archive_dir / "2026-07-03.md").exists()

    path = Path(wcfg["paths"]["day_log"])
    assert path.read_text().splitlines()[0] == "2026-07-04"

    from cortex.pacemaker import integration
    state = integration.load_state(marrow_conn)
    assert state.cortex_session_id == "sid-day2"
    assert state.cortex_session_date == "2026-07-04"


def test_run_wake_creates_ny_symlinks(marrow_conn, wcfg):
    caller = FakeCaller()
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=caller)

    from pathlib import Path
    ny = Path(wcfg["paths"]["ny_db_pages"])
    assert (ny / "day_log.md").is_symlink()
    assert (ny / "wishlist.md").is_symlink()
    assert (ny / "wishlist.md").resolve() == Path(wcfg["paths"]["wishlist_file"]).resolve()


class FailCaller:
    def __call__(self, prompt, cwd, resume_sid, cfg):
        raise wake.WakeError("boom")


def test_failed_wake_persists_rollover_and_preserves_archive(marrow_conn, wcfg):
    """A failed wake on a new day must still record the daily rollover so a
    retry does not re-archive; and the real archive from day 1 must never be
    clobbered by a blank shell (the 07-03 first-wake data-loss bug)."""
    good = FakeCaller(session_id="sid-day1")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY1, caller=good)
    day1_content = Path(wcfg["paths"]["day_log"]).read_text()

    # Day 2 first attempt: rebirth archives day1 + new_day, then caller fails.
    with pytest.raises(wake.WakeError):
        wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY2, caller=FailCaller())

    archive_dir = Path(wcfg["paths"]["day_log_archive_dir"])
    archived = archive_dir / "2026-07-03.md"
    assert archived.exists()
    assert archived.read_text() == day1_content

    from cortex.pacemaker import integration
    st = integration.load_state(marrow_conn)
    assert st.cortex_session_date == "2026-07-04"  # rollover persisted despite failure
    assert st.cortex_session_id is None

    # Day 2 retry: rebirth already recorded -> no re-archive, no clobber.
    good2 = FakeCaller(session_id="sid-day2")
    wake.run_wake(marrow_conn, wcfg, DECISION, now=DAY2 + timedelta(minutes=5), caller=good2)
    assert archived.read_text() == day1_content
    assert not (archive_dir / "2026-07-04.md").exists()
    assert integration.load_state(marrow_conn).cortex_session_id == "sid-day2"


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
    wake) but keeps date=today so the same day's log is never re-archived."""
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
    assert st.cortex_session_date == "2026-07-03"  # same day -> no re-archive

    # Third wake same day: fresh (resume None), still no archive of day1.
    good2 = FakeCaller(session_id="sid-3")
    wake.run_wake(marrow_conn, wcfg, DECISION,
                  now=later + timedelta(hours=1), caller=good2)
    assert good2.calls[0]["resume_sid"] is None
    assert integration.load_state(marrow_conn).cortex_session_id == "sid-3"
    assert not (Path(wcfg["paths"]["day_log_archive_dir"]) / "2026-07-03.md").exists()


# --------------------------------------------------------------------------- #
# Rotate (碎碎念 round-trip): a rotated/respawned resident window is a fresh
# brain and must receive the previous brain's handoff note.
# --------------------------------------------------------------------------- #

@pytest.fixture
def rot_cfg(wcfg, tmp_path):
    """wcfg + handoff note config + a written handoff file, mode=window."""
    cfg = dict(wcfg)
    cfg["wake"] = {**wcfg["wake"], "mode": "window"}
    cfg["paths"] = {**wcfg["paths"], "handoff_file": str(tmp_path / "handoff.md"),
                    "wake_state_file": str(tmp_path / "wake_state.json")}
    cfg["note"] = {"handoff_wake_kinds": ["rebirth", "rotate"],
                   "handoff_title": "碎碎念"}
    Path(cfg["paths"]["handoff_file"]).write_text("carry this to your next self")
    return cfg


def test_window_rotated_flag_path(monkeypatch, rot_cfg):
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.set_rotated(rot_cfg)
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
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
    monkeypatch.setattr(transcript, "newest", lambda cfg: Path("/t/new.jsonl"))
    assert wake._window_rotated(rot_cfg) is True


def test_window_rotated_dead_window_is_fresh(monkeypatch, rot_cfg):
    from cortex import wake_state, window
    wake_state.set_session_id(rot_cfg, "sid-1")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: False)
    assert wake._window_rotated(rot_cfg) is True


def test_window_unrotated_resume_stays_non_fresh(monkeypatch, rot_cfg):
    """Plain wake into a live, un-rotated window: same transcript, no flag ->
    NOT fresh (no 碎碎念; replay continuity lives in the window's own context)."""
    from cortex import wake_state, window, transcript
    wake_state.set_session_id(rot_cfg, "sid-1")
    wake_state.update(rot_cfg, transcript="/t/same.jsonl")
    monkeypatch.setattr(window, "is_running", lambda: True)
    monkeypatch.setattr(window, "_session_alive", lambda sid: True)
    monkeypatch.setattr(transcript, "newest", lambda cfg: Path("/t/same.jsonl"))
    assert wake._window_rotated(rot_cfg) is False


def test_window_wake_rotate_injects_handoff(monkeypatch, marrow_conn, rot_cfg):
    """Full window-branch: a rotated window (same local day) receives the 碎碎念."""
    monkeypatch.setattr(wake, "_window_rotated", lambda cfg: True)
    captured = {}
    def fake_window_wake(conn, cfg, note_text, now):
        captured["text"] = note_text
        return {"mode": "window", "session_id": None, "text": None}
    monkeypatch.setattr(wake, "_window_wake", fake_window_wake)
    # same-day second wake (not rebirth): seed today's session date
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)  # first wake seeds state
    captured.clear()
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1 + timedelta(hours=1))
    assert "碎碎念" in captured["text"]
    assert "carry this to your next self" in captured["text"]


def test_window_wake_rebirth_wins_over_rotate(monkeypatch, marrow_conn, rot_cfg):
    """Rebirth (new local date) sets its own kind; rotate is not re-evaluated."""
    called = {"rotated": False}
    def spy_rotated(cfg):
        called["rotated"] = True
        return True
    monkeypatch.setattr(wake, "_window_rotated", spy_rotated)
    captured = {}
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now: captured.update(text=t) or
                        {"mode": "window", "session_id": None, "text": None})
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)  # seed day1
    captured.clear()
    called["rotated"] = False
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY2)  # new date -> rebirth
    assert called["rotated"] is False           # rotate short-circuited by rebirth
    assert "碎碎念" in captured["text"]           # rebirth still delivers the handoff


def test_window_wake_unrotated_no_handoff(monkeypatch, marrow_conn, rot_cfg):
    """Un-rotated same-day wake: no 碎碎念 in the note."""
    monkeypatch.setattr(wake, "_window_rotated", lambda cfg: False)
    captured = {}
    monkeypatch.setattr(wake, "_window_wake",
                        lambda conn, cfg, t, now: captured.update(text=t) or
                        {"mode": "window", "session_id": None, "text": None})
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1)
    captured.clear()
    wake.run_wake(marrow_conn, rot_cfg, DECISION, now=DAY1 + timedelta(hours=1))
    assert "碎碎念" not in captured["text"]


def test_main_print_note_no_marrow_call(monkeypatch, marrow_conn, wcfg, capsys):
    monkeypatch.setattr(wake.config, "load", lambda: wcfg)
    monkeypatch.setattr(wake.db, "connect", lambda cfg: marrow_conn)

    rc = wake.main(["--print-note"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Now:" in out
