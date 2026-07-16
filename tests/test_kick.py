"""cortex.kick (P6): reason flag under flock + epoch, awake=flag-only, asleep=
flag + floor clear + sentinel kill + one detached tick. note.py renders + clears
the reason. All tick/sentinel spawns are stubbed — never kick the live cortex."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from cortex import db, kick, note, wake_state


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "cortex"
    (home / "state").mkdir(parents=True)
    return {
        "core": {"timezone": "Australia/Melbourne"},
        "paths": {
            "marrow_db": str(tmp_path / "marrow.db"),
            "cortex_home": str(home),
            "wake_state_file": str(home / "state" / "wake_state.json"),
            "wakeup_note_file": str(home / "wakeup_note.md"),
            "watchdog_pidfile": str(home / "state" / "watchdog.pid"),
            "wake_audit_log": str(home / "state" / "wake_audit.log"),
        },
        "kick": {
            "reason_reply": "watch: note #{id} got her reply",
            "reason_timeout": "watch: note #{id} silent {minutes}min",
            "reason_morning": "morning: she's up — flag cleared, day cadence",
        },
        "note": {"kick_header": "### Woke for"},
    }


@pytest.fixture
def _stub_spawn(monkeypatch):
    """Capture tick spawns instead of launching a real pacemaker_tick."""
    calls = []
    monkeypatch.setattr(kick, "_spawn_tick", lambda cfg: calls.append(cfg))
    return calls


def _ws(cfg) -> dict:
    return json.loads(wake_state.wake_state_path(cfg).read_text())


def test_kick_asleep_flags_and_ticks(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, next_wake_at="2026-07-17T09:00:00",
                      sentinel_pid=999999)
    r = kick.kick(cfg, "reply", id=7)
    assert r["ok"] and r["ticked"] and not r["awake"]
    assert len(_stub_spawn) == 1  # exactly one tick spawned
    d = _ws(cfg)
    assert d["kick_reasons"] == ["watch: note #7 got her reply"]
    assert "next_wake_at" not in d          # ledger cleared
    assert "sentinel_pid" not in d          # sentinel released


def test_kick_awake_flag_only_no_tick(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True, next_wake_at="2026-07-17T09:00:00")
    r = kick.kick(cfg, "morning")
    assert r["ok"] and r["awake"] and not r["ticked"]
    assert _stub_spawn == []                 # NO tick while awake
    d = _ws(cfg)
    assert d["kick_reasons"] == ["morning: she's up — flag cleared, day cadence"]
    assert d["next_wake_at"] == "2026-07-17T09:00:00"  # ledger untouched


def test_kick_bumps_gen_when_asleep(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, gen=3, state_id="abcd")
    kick.kick(cfg, "timeout", id=4, minutes=30)
    assert _ws(cfg)["gen"] == 4              # cancellation epoch bumped


def test_kick_awake_does_not_bump_gen(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True, gen=5, state_id="ef01")
    kick.kick(cfg, "reply", id=1)
    assert _ws(cfg)["gen"] == 5              # awake: no epoch change


def test_timeout_reason_renders_fields(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False)
    kick.kick(cfg, "timeout", id=9, minutes=45)
    assert _ws(cfg)["kick_reasons"] == ["watch: note #9 silent 45min"]


def test_reason_cap(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True)
    for i in range(12):
        kick.kick(cfg, "reply", id=i)
    assert len(_ws(cfg)["kick_reasons"]) == kick._MAX_REASONS


def test_note_renders_and_clears_kick_reasons(cfg, marrow_conn_for):
    conn = marrow_conn_for
    wake_state.update(cfg, awake=True,
                      kick_reasons=["watch: note #7 got her reply"])
    now = datetime.now(ZoneInfo("Australia/Melbourne"))
    data = note.gather(conn, cfg, now, consume_kick=True)
    assert data["kick_reasons"] == ["watch: note #7 got her reply"]
    text = note.render(cfg, now, data)
    assert "### Woke for" in text
    assert "watch: note #7 got her reply" in text
    # consumed: a second delivered render sees nothing
    assert "kick_reasons" not in _ws(cfg)
    data2 = note.gather(conn, cfg, now, consume_kick=True)
    assert data2["kick_reasons"] == []


def test_note_render_only_does_not_clear(cfg, marrow_conn_for):
    conn = marrow_conn_for
    wake_state.update(cfg, awake=True, kick_reasons=["morning: she's up"])
    now = datetime.now(ZoneInfo("Australia/Melbourne"))
    data = note.gather(conn, cfg, now, consume_kick=False)  # render-only
    assert data["kick_reasons"] == []                       # not surfaced
    assert _ws(cfg)["kick_reasons"] == ["morning: she's up"]  # NOT cleared


@pytest.fixture
def marrow_conn_for(cfg):
    conn = db.connect_path(__import__("pathlib").Path(cfg["paths"]["marrow_db"]))
    yield conn
    conn.close()
