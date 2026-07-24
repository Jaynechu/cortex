"""cortex.kick (reasons v2 + T1 kick_round carrier): under flock + epoch, asleep
= gen bump + floor clear + sentinel kill + one detached tick; awake = reason
queued + kick_round marked (idempotent) + one detached tick so the watchdog/
tick silence_action fires the carrier free-round now. Every kick appends a
rendered reason line (config [kick].reason_*) to wake_state for the next
delivered note; the kind also lands in the wake-audit log. All tick/sentinel
spawns are stubbed — never kick the live cortex."""
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
        "wake": {"signal_log": str(home / "state" / "wake_signal.log")},
        "kick": {
            "reason_reply": 'Msg #{id} replied: "{text}"',
            "reason_timeout": "Msg #{id} no reply in {minutes}min",
            "reason_morning": "She's up — day mode",
            "reason_note": "New note #{id}",
            "max_reasons": 8,
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


def _signal(cfg) -> str:
    from cortex import config
    p = config.wake_signal_log_path(cfg)
    return p.read_text() if p.exists() else ""


def test_kick_asleep_ticks_and_writes_reason(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, next_wake_at="2026-07-17T09:00:00",
                      sentinel_pid=999999)
    r = kick.kick(cfg, "reply", id=7, text="miss you")
    assert r["ok"] and r["ticked"] and not r["awake"]
    assert len(_stub_spawn) == 1  # exactly one tick spawned
    d = _ws(cfg)
    assert d["kick_reasons"] == ['Msg #7 replied: "miss you"']  # config template
    assert "next_wake_at" not in d          # ledger cleared
    assert "sentinel_pid" not in d          # sentinel released


def test_kick_bumps_gen_when_asleep(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, gen=3, state_id="abcd")
    kick.kick(cfg, "timeout", id=4, minutes=30)
    assert _ws(cfg)["gen"] == 4              # cancellation epoch bumped


def test_kick_awake_does_not_bump_gen(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True, gen=5, state_id="ef01")
    kick.kick(cfg, "reply", id=1)
    assert _ws(cfg)["gen"] == 5              # awake: no epoch change


def test_kind_and_fields_recorded_in_audit(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False)
    kick.kick(cfg, "timeout", id=9, minutes=45)
    audit = _audit(cfg)
    assert "kick" in audit and "timeout" in audit
    assert "id=9" in audit and "minutes=45" in audit
    assert _ws(cfg)["kick_reasons"] == ["Msg #9 no reply in 45min"]


def test_reason_list_capped_at_max(cfg, _stub_spawn):
    # Asleep reply kicks queue kick_reasons (delivered by the wake note); the list
    # is capped at max_reasons.
    cfg["kick"]["max_reasons"] = 3
    for i in range(5):
        wake_state.update(cfg, awake=False)
        kick.kick(cfg, "reply", id=i, text="x")
    reasons = _ws(cfg)["kick_reasons"]
    assert len(reasons) == 3                          # capped
    assert reasons[-1] == 'Msg #4 replied: "x"'       # newest kept


# --- night flag clear (P8) ---------------------------------------------------

def test_morning_kick_clears_flag_asleep(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False, mode="night")
    r = kick.kick(cfg, "morning")
    assert r["flag_cleared"] is True
    assert "mode" not in _ws(cfg)  # day cadence: flag gone


def test_morning_kick_clears_flag_awake(cfg, _stub_spawn):
    # Morning clears the flag even while awake (flag clear is separate from the
    # wake machinery). It also opens a carrier round like any awake kick.
    wake_state.update(cfg, awake=True, mode="night")
    r = kick.kick(cfg, "morning")
    assert r["awake"] is True and r["ticked"] is False
    assert r["flag_cleared"] is True
    assert "mode" not in _ws(cfg)
    assert r["round_opened"] is True


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


def test_asleep_interrupt_queues_reason(cfg, _stub_spawn):
    wake_state.update(cfg, awake=False)
    r = kick.kick(cfg, "reply", id=3, text="hi")
    assert r["ticked"] is True                # asleep path unchanged (ticks)
    d = _ws(cfg)
    assert d["kick_reasons"] == ['Msg #3 replied: "hi"']
    assert _signal(cfg) == ""                 # ear NOT written on the asleep path


# --- awake kick -> carrier free-round for ALL kinds (replaces retired F3/C2) --

def _assert_carrier(cfg, r, _stub_spawn, expect_reason):
    # Common assertions for an awake kick: queues the reason, marks kick_round,
    # spawns one tick (the tick's silence_action carrier is the visible round),
    # never bumps gen, never writes the ear directly.
    assert r["awake"] is True and r["ticked"] is False
    assert r["round_opened"] is True
    assert len(_stub_spawn) == 1                        # exactly one tick
    d = _ws(cfg)
    assert d["kick_reasons"] == [expect_reason]         # reason queued for render
    assert d["kick_round"] is True                      # carrier marked
    assert _signal(cfg) == ""                           # ear NOT written directly


def test_awake_reply_opens_carrier(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True, gen=5, state_id="ef01")
    r = kick.kick(cfg, "reply", id=7, text="miss you")
    _assert_carrier(cfg, r, _stub_spawn, 'Msg #7 replied: "miss you"')
    assert _ws(cfg)["gen"] == 5                          # awake: epoch untouched


def test_awake_timeout_opens_carrier(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True)
    r = kick.kick(cfg, "timeout", id=4, minutes=30)
    _assert_carrier(cfg, r, _stub_spawn, "Msg #4 no reply in 30min")


def test_awake_morning_opens_carrier(cfg, _stub_spawn):
    # Morning-awake opens a carrier round; the night flag still clears.
    wake_state.update(cfg, awake=True, mode="night")
    r = kick.kick(cfg, "morning")
    _assert_carrier(cfg, r, _stub_spawn, "She's up — day mode")
    assert r["flag_cleared"] is True
    assert "mode" not in _ws(cfg)                        # day cadence resumes


def test_awake_morning_no_flag_still_carrier(cfg, _stub_spawn):
    # Morning-awake with no night flag: no flag to clear, but the reason still
    # gets a carrier round.
    wake_state.update(cfg, awake=True)
    r = kick.kick(cfg, "morning")
    _assert_carrier(cfg, r, _stub_spawn, "She's up — day mode")
    assert r["flag_cleared"] is False


def test_second_kick_before_carrier_fires_queues_both_no_double_mark(cfg, _stub_spawn):
    # A kick landing while an earlier carrier is still pending: the marker stays
    # idempotent (no re-stamp, no redundant tick), but its own reason still
    # queues — so the eventual carrier round surfaces both.
    wake_state.update(cfg, awake=True)
    r1 = kick.kick(cfg, "reply", id=1, text="a")
    r2 = kick.kick(cfg, "timeout", id=2, minutes=10)
    assert r1["round_opened"] is True   # first kick marks + ticks
    assert r2["round_opened"] is False  # already pending -> no re-mark, no re-tick
    assert len(_stub_spawn) == 1        # only the marking kick spawns a tick
    d = _ws(cfg)
    assert d["kick_reasons"] == ['Msg #1 replied: "a"', "Msg #2 no reply in 10min"]
    assert d["kick_round"] is True


# --- F9: 'note' kind = ct-note drop -> immediate delivery -------------------

def test_note_kind_asleep_wakes(cfg, _stub_spawn):
    # ct note while asleep -> the note kind wakes cortex (tick + reason queued).
    wake_state.update(cfg, awake=False, next_wake_at="2026-07-17T09:00:00")
    r = kick.kick(cfg, "note", id=9)
    assert r["ok"] and r["ticked"] and not r["awake"]
    assert len(_stub_spawn) == 1
    assert _ws(cfg)["kick_reasons"] == ["New note #9"]


def test_note_kind_awake_opens_carrier(cfg, _stub_spawn):
    # ct note while awake -> carrier round (the visible round that renders the
    # note), reason queued.
    wake_state.update(cfg, awake=True)
    r = kick.kick(cfg, "note", id=9)
    _assert_carrier(cfg, r, _stub_spawn, "New note #9")
