from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cortex import config, db
from cortex.pacemaker import gates, integration
from cortex.pacemaker.core import PacemakerState

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
        next_floor_due_at=now + timedelta(minutes=60),
        last_wake_at=now,
        last_lie_down_at=now - timedelta(minutes=1),
        night_cap_key="2026-07-04",
        night_wake_count=1,
    )
    integration.save_state(conn, state)
    loaded = integration.load_state(conn)
    assert loaded.next_floor_due_at == state.next_floor_due_at
    assert loaded.last_wake_at == state.last_wake_at
    assert loaded.last_lie_down_at == state.last_lie_down_at
    assert loaded.night_cap_key == "2026-07-04"
    assert loaded.night_wake_count == 1


def test_load_state_empty_default(conn):
    assert integration.load_state(conn) == PacemakerState()


def test_load_state_legacy_desire_json_loads_gracefully(conn):
    # A pre-retirement row still carries desire/expect_reply/cortex_session_date
    # keys — they are ignored (not migrated), and loading must not crash.
    now = datetime.now(MEL)
    old_json = json.dumps({
        "desire": {"attachment": 0.1, "curiosity": 0.0, "worry": 0.0, "duty": 0.0,
                   "last_tick_at": now.isoformat()},
        "expect_reply": {"pending": True, "sent_at": now.isoformat(),
                         "checks_done": 3, "tone_level": 2},
        "cortex_session_date": "2026-07-04",
        "next_floor_due_at": now.isoformat(),
        "last_wake_at": None,
    })
    conn.execute(
        "INSERT INTO ct_pacemaker_state (id, state, updated_at) VALUES (1, ?, ?)",
        (old_json, db.utcnow_iso()),
    )
    conn.commit()
    state = integration.load_state(conn)
    assert state.next_floor_due_at is not None
    assert state.last_lie_down_at is None
    assert state.night_cap_key is None
    assert state.night_wake_count == 0
    # A save drops the stale desire/expect_reply/cortex_session_date keys.
    integration.save_state(conn, state)
    raw = json.loads(conn.execute(
        "SELECT state FROM ct_pacemaker_state WHERE id = 1").fetchone()["state"])
    assert "desire" not in raw
    assert "expect_reply" not in raw
    assert "cortex_session_date" not in raw


# --- lie_down ---------------------------------------------------------------

def test_lie_down_sets_floor_and_preserves_other_fields(conn, cfg):
    now = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)
    prior = PacemakerState(
        last_wake_at=now,
        cortex_session_id="sid-1",
    )
    integration.save_state(conn, prior)

    integration.lie_down(conn, cfg, now=now, rng=random.Random(1))

    state = integration.load_state(conn)
    assert state.last_lie_down_at == now
    floor_delta_min = (state.next_floor_due_at - now).total_seconds() / 60.0
    assert 10.0 <= floor_delta_min <= 55.0
    # other fields untouched
    assert state.last_wake_at == now
    assert state.cortex_session_id == "sid-1"


def test_lie_down_explicit_minutes_sets_exact_next_wake(conn, cfg):
    now = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)
    integration.save_state(conn, PacemakerState())
    integration.lie_down(conn, cfg, now=now, rng=random.Random(1), minutes=25)
    state = integration.load_state(conn)
    assert state.next_floor_due_at == now + timedelta(minutes=25)


def test_lie_down_explicit_minutes_clamped_to_window(conn, cfg):
    now = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)
    integration.save_state(conn, PacemakerState())
    integration.lie_down(conn, cfg, now=now, rng=random.Random(1), minutes=999)
    state = integration.load_state(conn)
    # clamps to floor_max_min (default 55)
    assert state.next_floor_due_at == now + timedelta(minutes=55)


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
    day = now.replace(hour=1, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc)
    cap = cfg["gates"]["daily_budget"]["tokens"]
    # a FINISHED window (run peaks over cap, then a lower window closes it) puts
    # Cortex Today over the cap
    conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)",
        [(day.isoformat(), cap + 5_000),
         ((day + timedelta(minutes=5)).isoformat(), 3_000)])
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


def test_daily_budget_single_open_window_under_cap(conn, cfg):
    """One window's occupancy (a single monotonic run, still the current window)
    is NOT a finished final and is counted only via the live hint. With no live
    hint published, Cortex Today = 0 even if the occupancy row is huge -> not
    gated."""
    now = datetime(2026, 7, 8, 12, 0, tzinfo=MEL)
    start_utc = now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc).isoformat()
    cap = cfg["gates"]["daily_budget"]["tokens"]
    conn.execute("INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)",
                 (start_utc, cap * 3))  # trailing (open) window -> not a finished final
    conn.commit()
    integration.save_state(conn, PacemakerState(next_floor_due_at=now - timedelta(minutes=1)))
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is True  # 0 today (no finished final, no live hint)
    assert not any(g.name == "daily_budget" for g in decision["gated_by"])


def test_daily_budget_finished_window_final_over_cap_gates(conn, cfg):
    """A FINISHED window (its run peaks over cap, then a later window's lower
    occupancy marks the drop that closes it) contributes its final to Cortex
    Today -> gate trips."""
    now = datetime(2026, 7, 8, 12, 0, tzinfo=MEL)
    day = now.replace(hour=1, minute=0, second=0, microsecond=0).astimezone(
        timezone.utc)
    cap = cfg["gates"]["daily_budget"]["tokens"]
    rows = [
        # window 1 peaks over cap, then window 2 restarts lower (drop closes w1)
        ((day).isoformat(), cap + 5_000),
        ((day + timedelta(minutes=5)).isoformat(), 3_000),
    ]
    conn.executemany(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, tokens) VALUES (?,1,0,?)", rows)
    conn.commit()
    integration.save_state(conn, PacemakerState(next_floor_due_at=now - timedelta(minutes=1)))
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is False
    assert any(g.name == "daily_budget" for g in decision["gated_by"])


def test_dry_run_marks_schedule_fired_no_refire(conn, cfg):
    """Regression: under dry_run a due duty must be marked fired so it does not
    re-fire every 5-min tick until midnight (run_wake, the live marker, is never
    called). _mark_dry_run_schedule_fired records the fire; the next tick's
    due_duties then skips it."""
    from cortex import pacemaker_tick

    cfg["pacemaker"]["dry_run"] = True
    cfg["schedule"] = [{"name": "review+plan", "time": "20:30", "enabled": True}]

    now1 = datetime(2026, 7, 8, 20, 31, tzinfo=MEL)
    decision1 = integration.run_tick(conn, cfg, now=now1, rng=random.Random(1))
    assert decision1["wake"] is True
    assert any(r.kind == "schedule" for r in decision1["reasons"])

    # tick entry marks the fired duty under dry_run
    pacemaker_tick._mark_dry_run_schedule_fired(conn, decision1, now1)
    assert integration.load_schedule_fired(conn) == {"review+plan": "2026-07-08"}

    # a later tick the same day no longer re-fires the duty
    now2 = datetime(2026, 7, 8, 20, 36, tzinfo=MEL)
    decision2 = integration.run_tick(conn, cfg, now=now2, rng=random.Random(1))
    assert not any(r.kind == "schedule" for r in decision2["reasons"])


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
