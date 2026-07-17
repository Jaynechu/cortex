"""cortex.kick (P6, reasons retired): under flock + epoch, asleep = gen bump +
floor clear + sentinel kill + one detached tick; awake = audit-only no-op. The
kind lands in the wake-audit log ONLY — never in wake_state or the note. All
tick/sentinel spawns are stubbed — never kick the live cortex."""
from __future__ import annotations

import json

import pytest

from cortex import kick, wake_state


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
    }


@pytest.fixture
def _stub_spawn(monkeypatch):
    """Capture tick spawns instead of launching a real pacemaker_tick."""
    calls = []
    monkeypatch.setattr(kick, "_spawn_tick", lambda cfg: calls.append(cfg))
    return calls


def _ws(cfg) -> dict:
    return json.loads(wake_state.wake_state_path(cfg).read_text())


def _audit(cfg) -> str:
    from cortex import config
    return config.wake_audit_log_path(cfg).read_text()


def test_kick_asleep_ticks_no_reason(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, next_wake_at="2026-07-17T09:00:00",
                      sentinel_pid=999999)
    r = kick.kick(cfg, "reply", id=7)
    assert r["ok"] and r["ticked"] and not r["awake"]
    assert len(_stub_spawn) == 1  # exactly one tick spawned
    d = _ws(cfg)
    assert "kick_reasons" not in d          # reasons retired
    assert "next_wake_at" not in d          # ledger cleared
    assert "sentinel_pid" not in d          # sentinel released


def test_kick_awake_audit_only_no_tick(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True, next_wake_at="2026-07-17T09:00:00")
    r = kick.kick(cfg, "morning")
    assert r["ok"] and r["awake"] and not r["ticked"]
    assert _stub_spawn == []                 # NO tick while awake
    d = _ws(cfg)
    assert "kick_reasons" not in d           # no reason written
    assert d["next_wake_at"] == "2026-07-17T09:00:00"  # ledger untouched


def test_kick_bumps_gen_when_asleep(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, gen=3, state_id="abcd")
    kick.kick(cfg, "timeout", id=4, minutes=30)
    assert _ws(cfg)["gen"] == 4              # cancellation epoch bumped


def test_kick_awake_does_not_bump_gen(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True, gen=5, state_id="ef01")
    kick.kick(cfg, "reply", id=1)
    assert _ws(cfg)["gen"] == 5              # awake: no epoch change


def test_kind_and_fields_recorded_in_audit_only(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False)
    kick.kick(cfg, "timeout", id=9, minutes=45)
    audit = _audit(cfg)
    assert "kick" in audit and "timeout" in audit
    assert "id=9" in audit and "minutes=45" in audit
    assert "kick_reasons" not in _ws(cfg)


# --- night flag clear (P8) ---------------------------------------------------

def test_morning_kick_clears_flag_asleep(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, mode="night")
    r = kick.kick(cfg, "morning")
    assert r["flag_cleared"] is True
    assert "mode" not in _ws(cfg)  # day cadence: flag gone


def test_morning_kick_clears_flag_awake(cfg, _stub_spawn):
    # Morning clears the flag even while awake (flag clear is separate from the
    # awake audit-only no-op for wake machinery).
    wake_state.update(cfg, awake=True, mode="night")
    r = kick.kick(cfg, "morning")
    assert r["awake"] is True and r["ticked"] is False
    assert r["flag_cleared"] is True
    assert "mode" not in _ws(cfg)
    assert _stub_spawn == []  # no tick while awake


def test_midnight_reply_kick_keeps_flag(cfg, _stub_spawn):
    # A watch reply/timeout mid-night wakes cortex but does NOT clear the flag
    # (dawdling is not morning).
    wake_state.update(cfg, awake=False, mode="night")
    r = kick.kick(cfg, "reply", id=3)
    assert r.get("flag_cleared") is False
    assert _ws(cfg)["mode"] == "night"  # flag survives


def test_morning_kick_no_flag_is_noop(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False)  # day, no flag
    r = kick.kick(cfg, "morning")
    assert r["flag_cleared"] is False
