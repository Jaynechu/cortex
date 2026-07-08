from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cortex import config, db
from cortex.pacemaker import gates, integration
from cortex.pacemaker.core import PacemakerState
from cortex.pacemaker.desire import DesireState
from cortex.pacemaker.expect_reply import ExpectReplyState

MEL = ZoneInfo("Australia/Melbourne")


@pytest.fixture
def cfg(tmp_path):
    c = config.load(path=tmp_path / "no_such_config.toml")
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["affect_flag_file"] = str(tmp_path / "affect_flag.json")
    c["paths"]["self_schedule_file"] = str(tmp_path / "self_schedule.json")
    return c


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg)
    yield c
    c.close()


# --- state round trip ------------------------------------------------------

def test_state_round_trip(conn):
    now = datetime.now(MEL)
    state = PacemakerState(
        desire=DesireState(attachment=0.4, curiosity=0.1, worry=0.2, duty=0.3, last_tick_at=now),
        expect_reply=ExpectReplyState(pending=True, sent_at=now, last_check_at=now,
                                      checks_done=2, tone_level=1),
        next_floor_due_at=now + timedelta(minutes=60),
        last_wake_at=now,
        last_lie_down_at=now - timedelta(minutes=1),
        night_cap_key="2026-07-04",
        night_wake_count=1,
    )
    integration.save_state(conn, state)
    loaded = integration.load_state(conn)
    assert loaded.desire.attachment == pytest.approx(0.4)
    assert loaded.expect_reply.pending is True
    assert loaded.expect_reply.checks_done == 2
    assert loaded.next_floor_due_at == state.next_floor_due_at
    assert loaded.last_wake_at == state.last_wake_at
    assert loaded.last_lie_down_at == state.last_lie_down_at
    assert loaded.night_cap_key == "2026-07-04"
    assert loaded.night_wake_count == 1


def test_load_state_empty_default(conn):
    assert integration.load_state(conn) == PacemakerState()


def test_load_state_old_json_without_new_fields_still_loads(conn):
    now = datetime.now(MEL)
    old_json = json.dumps({
        "desire": {"attachment": 0.1, "curiosity": 0.0, "worry": 0.0, "duty": 0.0,
                   "last_tick_at": now.isoformat()},
        "expect_reply": {"pending": False, "sent_at": None, "last_check_at": None,
                         "checks_done": 0, "tone_level": 0},
        "next_floor_due_at": None,
        "last_wake_at": None,
    })
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)",
        (old_json, db.utcnow_iso()),
    )
    conn.commit()
    state = integration.load_state(conn)
    assert state.desire.attachment == pytest.approx(0.1)
    assert state.last_lie_down_at is None
    assert state.night_cap_key is None
    assert state.night_wake_count == 0


# --- lie_down ---------------------------------------------------------------

def test_lie_down_sets_floor_and_preserves_other_fields(conn, cfg):
    now = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)
    prior = PacemakerState(
        desire=DesireState(attachment=0.4, curiosity=0.1, worry=0.2, duty=0.3, last_tick_at=now),
        last_wake_at=now,
        cortex_session_id="sid-1",
        cortex_session_date="2026-07-04",
    )
    integration.save_state(conn, prior)

    integration.lie_down(conn, cfg, now=now, rng=random.Random(1))

    state = integration.load_state(conn)
    assert state.last_lie_down_at == now
    floor_delta_min = (state.next_floor_due_at - now).total_seconds() / 60.0
    assert 10.0 <= floor_delta_min <= 55.0
    # other fields untouched
    assert state.desire.attachment == pytest.approx(0.4)
    assert state.last_wake_at == now
    assert state.cortex_session_id == "sid-1"
    assert state.cortex_session_date == "2026-07-04"


# --- context builder -------------------------------------------------------

def test_build_context_active_and_last_chat(conn, cfg):
    now = datetime.now(MEL)
    recent = (now - timedelta(minutes=2)).astimezone(timezone.utc)
    conn.execute("INSERT INTO ct_activity (ts, sid, channel) VALUES (?, 's1', 'wx')",
                 (recent.isoformat(),))
    conn.commit()
    ctx = integration.build_context(conn, cfg, now, PacemakerState())
    assert ctx["active_session"] is True
    assert ctx["last_real_chat_at"] is not None
    assert ctx["events"] == []


def test_build_context_inactive_when_stale(conn, cfg):
    now = datetime.now(MEL)
    old = (now - timedelta(minutes=30)).astimezone(timezone.utc)
    conn.execute("INSERT INTO ct_activity (ts, sid, channel) VALUES (?, 's1', 'wx')",
                 (old.isoformat(),))
    conn.commit()
    ctx = integration.build_context(conn, cfg, now, PacemakerState())
    assert ctx["active_session"] is False


def test_build_context_reads_flag_and_schedule_files(conn, cfg, tmp_path):
    now = datetime.now(MEL)
    (tmp_path / "affect_flag.json").write_text(json.dumps({"kind": "upset"}))
    due = (now - timedelta(minutes=1)).isoformat()
    (tmp_path / "self_schedule.json").write_text(json.dumps([{"due_at": due, "note": "check ate"}]))
    ctx = integration.build_context(conn, cfg, now, PacemakerState())
    assert ctx["affect_flag"] == {"kind": "upset"}
    assert len(ctx["self_scheduled"]) == 1
    assert isinstance(ctx["self_scheduled"][0]["due_at"], datetime)


def test_self_scheduled_tolerates_bare_dict(cfg, tmp_path):
    """A bare dict (not wrapped in a list) is read as a single-item list."""
    now = datetime.now(MEL)
    due = (now - timedelta(minutes=1)).isoformat()
    (tmp_path / "self_schedule.json").write_text(json.dumps({"due_at": due, "intent": "check in"}))
    out = integration._self_scheduled(cfg)
    assert len(out) == 1
    assert out[0]["intent"] == "check in"
    assert isinstance(out[0]["due_at"], datetime)


# --- run_tick orchestration ------------------------------------------------

def test_first_tick_fires_floor_and_persists(conn, cfg):
    now = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)  # outside night window
    rng = random.Random(1)
    decision = integration.run_tick(conn, cfg, now=now, rng=rng)
    assert decision["wake"] is True
    assert any(r.kind == "floor" for r in decision["reasons"])
    row = conn.execute("SELECT wake, dry_run FROM ct_wake_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["wake"] == 1
    assert row["dry_run"] == 1
    state = integration.load_state(conn)
    assert state.next_floor_due_at is not None
    assert state.last_wake_at == now


def test_second_tick_no_wake_while_floor_not_due(conn, cfg):
    now1 = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)
    integration.run_tick(conn, cfg, now=now1, rng=random.Random(1))
    # 5 min later the floor (drawn >=10min) is not due and nothing else fires,
    # so no wake — and no gate is involved (no reasons at all).
    now2 = now1 + timedelta(minutes=5)
    decision = integration.run_tick(conn, cfg, now=now2, rng=random.Random(1))
    assert decision["wake"] is False
    assert decision["reasons"] == []
    assert decision["gated_by"] == []
    assert conn.execute("SELECT COUNT(*) AS n FROM ct_wake_log").fetchone()["n"] == 2


def test_schedule_due_wakes_and_pierces_night(conn, cfg):
    # 23:30 (night, cap 0) — a due duty still wakes; floor would be silenced.
    now = datetime(2026, 7, 8, 23, 30, tzinfo=MEL)
    cfg["schedule"] = [{"name": "review+plan", "time": "20:30", "enabled": True}]
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is True
    assert any(r.kind == "schedule" for r in decision["reasons"])
    assert decision["gated_by"] == []


def test_schedule_disabled_does_not_wake(conn, cfg):
    now = datetime(2026, 7, 8, 21, 0, tzinfo=MEL)
    cfg["schedule"] = [{"name": "wp", "time": "08:00", "enabled": False}]
    integration.save_state(conn, PacemakerState(next_floor_due_at=now + timedelta(hours=1)))
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert not any(r.kind == "schedule" for r in decision["reasons"])


def test_schedule_fired_persists_across_save_state(conn, cfg):
    integration.mark_schedule_fired(conn, "review+plan", "2026-07-08")
    # A plain tick save must not wipe the side-channel key.
    integration.save_state(conn, PacemakerState(night_wake_count=3))
    assert integration.load_schedule_fired(conn) == {"review+plan": "2026-07-08"}


def test_daily_budget_gates_floor_and_schedule_pierces(conn, cfg):
    now = datetime(2026, 7, 8, 12, 0, tzinfo=MEL)  # daytime, outside night
    start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc).isoformat()
    conn.execute("INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)",
                 (start_utc, cfg["gates"]["daily_budget"]["tokens"]))
    conn.commit()
    # floor is gated by daily budget
    integration.save_state(conn, PacemakerState(next_floor_due_at=now - timedelta(minutes=1)))
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is False
    assert any(g.name == "daily_budget" for g in decision["gated_by"])
    # schedule pierces the same budget
    cfg["schedule"] = [{"name": "wp", "time": "08:00", "enabled": True}]
    decision2 = integration.run_tick(conn, cfg, now=now + timedelta(minutes=5), rng=random.Random(1))
    assert decision2["wake"] is True
    assert any(r.kind == "schedule" for r in decision2["reasons"])


def test_daily_budget_uses_net_tokens_over_total(conn, cfg):
    """The gate sums COALESCE(net_tokens, tokens): a row with net_tokens set
    counts NET spend, not the (much larger) total occupancy in `tokens`."""
    now = datetime(2026, 7, 8, 12, 0, tzinfo=MEL)
    start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc).isoformat()
    cap = cfg["gates"]["daily_budget"]["tokens"]
    # total occupancy is way over cap, but net spend is a small fraction of it
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens, net_tokens) VALUES (?,1,0,?,?)",
        (start_utc, cap * 3, 100))
    conn.commit()
    integration.save_state(conn, PacemakerState(next_floor_due_at=now - timedelta(minutes=1)))
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is True  # net (100) is far under cap -> not gated
    assert not any(g.name == "daily_budget" for g in decision["gated_by"])


def test_daily_budget_falls_back_to_tokens_when_net_missing(conn, cfg):
    """A pre-migration row (net_tokens NULL) degrades to `tokens` — unchanged
    behaviour for old rows."""
    now = datetime(2026, 7, 8, 12, 0, tzinfo=MEL)
    start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc).isoformat()
    conn.execute("INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)",
                 (start_utc, cfg["gates"]["daily_budget"]["tokens"]))
    conn.commit()
    integration.save_state(conn, PacemakerState(next_floor_due_at=now - timedelta(minutes=1)))
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is False
    assert any(g.name == "daily_budget" for g in decision["gated_by"])


def test_night_mode_gates_floor_wake(conn, cfg):
    now = datetime(2026, 7, 4, 2, 0, tzinfo=MEL)  # inside default 23:00-06:00 window
    night_key = gates.night_key(cfg, now)
    state = PacemakerState(
        next_floor_due_at=now - timedelta(minutes=1),  # force a floor trigger
        night_cap_key=night_key,
        night_wake_count=0,  # cap is 0 -> any self-wake is silenced at night
    )
    integration.save_state(conn, state)
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is False
    assert any(g.name == "night-mode" for g in decision["gated_by"])
