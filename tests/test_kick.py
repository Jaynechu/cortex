"""cortex.kick (P6 + reasons v2): under flock + epoch, asleep = gen bump + floor
clear + sentinel kill + one detached tick; awake = reason-flag only. Every kick
appends a rendered reason line (config [kick].reason_*) to wake_state for the
next delivered note; the kind also lands in the wake-audit log. All tick/sentinel
spawns are stubbed — never kick the live cortex."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cortex import kick, wake_state


def _future_iso(minutes: int = 30) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


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


def test_kick_awake_interrupt_signals_not_queued(cfg, _stub_spawn):
    # P12/C2: a reply/timeout kick reaching an AWAKE cortex with a LIVE wait rides
    # the ear (wake_signal.log) instead of queuing in kick_reasons (a queued
    # reason would duplicate at the next note render) — and never ticks.
    wake_state.update(cfg, awake=True, next_wake_at="2026-07-17T09:00:00",
                      silence_wait_until=_future_iso())
    r = kick.kick(cfg, "timeout", id=4, minutes=30)
    assert r["ok"] and r["awake"] and not r["ticked"]
    assert r["signalled"] is True
    assert _stub_spawn == []                 # NO tick while awake
    d = _ws(cfg)
    assert "kick_reasons" not in d           # NOT queued (no note duplication)
    assert "Msg #4 no reply in 30min" in _signal(cfg)  # rode the ear
    assert d["next_wake_at"] == "2026-07-17T09:00:00"  # ledger untouched


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
    # is capped at max_reasons. (Awake interrupt kicks ride the ear, not the list.)
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
    # wake machinery). With no live wait it also opens an F3 carrier round.
    wake_state.update(cfg, awake=True, mode="night")
    r = kick.kick(cfg, "morning")
    assert r["awake"] is True and r["ticked"] is False
    assert r["flag_cleared"] is True
    assert "mode" not in _ws(cfg)
    assert r["round_opened"] is True          # F3 carrier (no live wait)


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


# --- P12: watch awake-interrupt (clear wait + ride the ear) ------------------

def test_awake_reply_clears_wait_and_signals(cfg, _stub_spawn):
    # C2 (P12, untouched by F3): awake reply kick with a LIVE wait voids the wait
    # premise — clears silence_wait_until in the SAME lock and rides the ear.
    wake_state.update(cfg, awake=True, silence_wait_until=_future_iso())
    r = kick.kick(cfg, "reply", id=7, text="miss you")
    assert r["awake"] and r["wait_cleared"] is True and r["signalled"] is True
    assert r["round_opened"] is False                 # C2: no carrier round
    assert _stub_spawn == []                           # C2: no tick
    d = _ws(cfg)
    assert "silence_wait_until" not in d               # wait voided
    assert "kick_reasons" not in d                     # not queued
    assert 'Msg #7 replied: "miss you"' in _signal(cfg)


def test_awake_morning_live_wait_queues_no_carrier(cfg, _stub_spawn):
    # Morning is not an interrupt kind: with a LIVE wait it clears the flag,
    # leaves the wait intact, queues the reason and opens NO carrier (the wait's
    # own expiry free-round surfaces it) — never writes the ear.
    until = _future_iso()
    wake_state.update(cfg, awake=True, mode="night", silence_wait_until=until)
    r = kick.kick(cfg, "morning")
    assert r["flag_cleared"] is True
    assert r["wait_cleared"] is False and r["signalled"] is False
    assert r["round_opened"] is False
    assert _stub_spawn == []
    d = _ws(cfg)
    assert d["silence_wait_until"] == until            # wait untouched
    assert d["kick_reasons"] == ["She's up — day mode"]
    assert _signal(cfg) == ""


def test_asleep_interrupt_queues_not_signals(cfg, _stub_spawn):
    # Asleep path is unchanged: the reason queues in kick_reasons (delivered by
    # the wake note), the ear is NOT written.
    wake_state.update(cfg, awake=False, silence_wait_until="2026-07-17T09:00:00")
    r = kick.kick(cfg, "reply", id=3, text="hi")
    assert r["ticked"] is True                # asleep path unchanged (ticks)
    d = _ws(cfg)
    assert d["kick_reasons"] == ['Msg #3 replied: "hi"']
    assert _signal(cfg) == ""                 # ear NOT written on the asleep path


# --- F3: awake + NO live wait -> carrier free-round for ALL kinds ------------

def _assert_carrier(cfg, r, _stub_spawn, expect_reason):
    # Common assertions for an awake + no-live-wait carrier kick: no ear, queues
    # the reason, stamps an EXPIRED wait, spawns one tick (the tick's wait-expiry
    # free-round is the carrier round), never bumps gen.
    assert r["awake"] is True and r["ticked"] is False
    assert r["round_opened"] is True
    assert r["signalled"] is False and r["wait_cleared"] is False
    assert len(_stub_spawn) == 1                        # exactly one tick
    d = _ws(cfg)
    assert d["kick_reasons"] == [expect_reason]         # reason queued for render
    assert _signal(cfg) == ""                           # ear NOT written
    until = datetime.fromisoformat(d["silence_wait_until"])
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    assert until <= datetime.now(timezone.utc)          # expired -> fires now


def test_awake_reply_no_wait_opens_carrier(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True, gen=5, state_id="ef01")
    r = kick.kick(cfg, "reply", id=7, text="miss you")
    _assert_carrier(cfg, r, _stub_spawn, 'Msg #7 replied: "miss you"')
    assert _ws(cfg)["gen"] == 5                          # awake: epoch untouched


def test_awake_timeout_no_wait_opens_carrier(cfg, _stub_spawn):
    wake_state.update(cfg, awake=True)
    r = kick.kick(cfg, "timeout", id=4, minutes=30)
    _assert_carrier(cfg, r, _stub_spawn, "Msg #4 no reply in 30min")


def test_awake_morning_no_wait_opens_carrier(cfg, _stub_spawn):
    # The dead-zone F3 fixes: morning-awake used to only queue a reason no round
    # ever rendered. Now it opens a carrier round; the night flag still clears.
    wake_state.update(cfg, awake=True, mode="night")
    r = kick.kick(cfg, "morning")
    _assert_carrier(cfg, r, _stub_spawn, "She's up — day mode")
    assert r["flag_cleared"] is True
    assert "mode" not in _ws(cfg)                        # day cadence resumes


def test_awake_timeout_live_wait_rides_ear_no_carrier(cfg, _stub_spawn):
    # C2 sibling: interrupt + LIVE wait clears the wait and rides the ear, never
    # opens a carrier round.
    wake_state.update(cfg, awake=True, silence_wait_until=_future_iso())
    r = kick.kick(cfg, "timeout", id=4, minutes=30)
    assert r["signalled"] is True and r["wait_cleared"] is True
    assert r["round_opened"] is False
    assert _stub_spawn == []
    assert "kick_reasons" not in _ws(cfg)
    assert "Msg #4 no reply in 30min" in _signal(cfg)


def test_awake_morning_no_wait_no_flag_still_carrier(cfg, _stub_spawn):
    # Morning-awake with no night flag and no live wait: no flag to clear, but
    # the reason still gets a carrier round.
    wake_state.update(cfg, awake=True)
    r = kick.kick(cfg, "morning")
    _assert_carrier(cfg, r, _stub_spawn, "She's up — day mode")
    assert r["flag_cleared"] is False


# --- F9: 'note' kind = ct-note drop -> immediate delivery -------------------

def test_note_kind_asleep_wakes(cfg, _stub_spawn):
    # ct note while asleep -> the note kind wakes cortex (tick + reason queued).
    wake_state.update(cfg, awake=False, next_wake_at="2026-07-17T09:00:00")
    r = kick.kick(cfg, "note", id=9)
    assert r["ok"] and r["ticked"] and not r["awake"]
    assert len(_stub_spawn) == 1
    assert _ws(cfg)["kick_reasons"] == ["New note #9"]


def test_note_kind_awake_idle_opens_carrier(cfg, _stub_spawn):
    # ct note while awake-idle (no live wait) -> F3 carrier round (the visible
    # round that renders the note), reason queued, never rides the ear.
    wake_state.update(cfg, awake=True)
    r = kick.kick(cfg, "note", id=9)
    _assert_carrier(cfg, r, _stub_spawn, "New note #9")


def test_note_kind_awake_live_wait_queues_no_carrier(cfg, _stub_spawn):
    # ct note while awake + LIVE wait -> reason queued, rides the wait-expiry
    # free-round (no new carrier, no ear write; note is not an interrupt).
    wake_state.update(cfg, awake=True, silence_wait_until=_future_iso())
    r = kick.kick(cfg, "note", id=9)
    assert r["awake"] is True and r["ticked"] is False
    assert r["round_opened"] is False and r["signalled"] is False
    assert _stub_spawn == []
    d = _ws(cfg)
    assert d["kick_reasons"] == ["New note #9"]
    assert d["silence_wait_until"]                      # wait untouched
    assert _signal(cfg) == ""                           # ear NOT written
