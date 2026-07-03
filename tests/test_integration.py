from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cortex import config, db
from cortex.pacemaker import integration
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


def _add_audit_table(conn):
    conn.execute(
        "CREATE TABLE audit_log (target_table TEXT, action TEXT, summary TEXT,"
        " occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')))"
    )
    conn.commit()


def _add_cost(conn, tokens_each, when_utc):
    summary = f"model=opus fmt=json in={tokens_each} out={tokens_each} cache_read=0 cache_write=0"
    conn.execute(
        "INSERT INTO audit_log (target_table, action, summary, occurred_at) VALUES (?,?,?,?)",
        ("llm_usage", "llm_call_cost", summary, when_utc.strftime("%Y-%m-%dT%H:%M:%SZ")),
    )
    conn.commit()


# --- token meter -----------------------------------------------------------

def test_token_meter_no_table(conn, cfg):
    cfg["pacemaker"]["token_meter"]["daily_budget_tokens"] = 1000
    now = datetime.now(MEL)
    assert integration.token_budget_remaining_fraction(conn, cfg, now) == 1.0


def test_token_meter_zero_budget_is_noop(conn, cfg):
    _add_audit_table(conn)
    _add_cost(conn, 500, datetime.now(timezone.utc))
    now = datetime.now(MEL)
    assert integration.token_budget_remaining_fraction(conn, cfg, now) == 1.0


def test_token_meter_counts_window(conn, cfg):
    _add_audit_table(conn)
    now = datetime.now(MEL)
    now_utc = now.astimezone(timezone.utc)
    _add_cost(conn, 100, now_utc)              # 100+100 = 200 in window
    _add_cost(conn, 50, now_utc)               # +100 = 300
    _add_cost(conn, 999, now_utc - timedelta(hours=48))  # outside 24h window
    cfg["pacemaker"]["token_meter"]["daily_budget_tokens"] = 1000
    frac = integration.token_budget_remaining_fraction(conn, cfg, now)
    assert frac == pytest.approx(1 - 300 / 1000)


# --- state round trip ------------------------------------------------------

def test_state_round_trip(conn):
    now = datetime.now(MEL)
    state = PacemakerState(
        desire=DesireState(attachment=0.4, curiosity=0.1, worry=0.2, duty=0.3, last_tick_at=now),
        expect_reply=ExpectReplyState(pending=True, sent_at=now, last_check_at=now,
                                      checks_done=2, tone_level=1),
        next_floor_due_at=now + timedelta(minutes=60),
        last_wake_at=now,
    )
    integration.save_state(conn, state)
    loaded = integration.load_state(conn)
    assert loaded.desire.attachment == pytest.approx(0.4)
    assert loaded.expect_reply.pending is True
    assert loaded.expect_reply.checks_done == 2
    assert loaded.next_floor_due_at == state.next_floor_due_at
    assert loaded.last_wake_at == state.last_wake_at


def test_load_state_empty_default(conn):
    assert integration.load_state(conn) == PacemakerState()


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


# --- run_tick orchestration ------------------------------------------------

def test_first_tick_fires_floor_and_persists(conn, cfg):
    now = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)  # outside fatigue window
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


def test_second_tick_resumes_and_cools_down(conn, cfg):
    now1 = datetime(2026, 7, 4, 10, 0, tzinfo=MEL)
    integration.run_tick(conn, cfg, now=now1, rng=random.Random(1))
    # 5 min later: floor not due, cooldown (45min) blocks any wake
    now2 = now1 + timedelta(minutes=5)
    decision = integration.run_tick(conn, cfg, now=now2, rng=random.Random(1))
    assert decision["wake"] is False
    assert conn.execute("SELECT COUNT(*) AS n FROM ct_wake_log").fetchone()["n"] == 2


def test_fatigue_window_gates_wake(conn, cfg):
    now = datetime(2026, 7, 4, 2, 0, tzinfo=MEL)  # inside 23:30-07:00 window
    decision = integration.run_tick(conn, cfg, now=now, rng=random.Random(1))
    assert decision["wake"] is False
    assert any(g.name == "fatigue-window" for g in decision["gated_by"])
