from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    }
    cfg["marrow"] = {"repo_dir": "", "venv_python": "", "call_timeout_s": 5}
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
    assert "Trigger: none" in text
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


def test_main_print_bulletin_no_marrow_call(monkeypatch, marrow_conn, wcfg, capsys):
    monkeypatch.setattr(wake.config, "load", lambda: wcfg)
    monkeypatch.setattr(wake.db, "connect", lambda cfg: marrow_conn)

    rc = wake.main(["--print-bulletin"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Now:" in out
