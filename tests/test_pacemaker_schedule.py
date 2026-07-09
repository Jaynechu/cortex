from datetime import datetime, timedelta, timezone

from cortex.pacemaker import schedule
from cortex.pacemaker.triggers import evaluate

TZ = timezone(timedelta(hours=10))


def _entry(name="review+plan", t="20:30", enabled=True, prompt_path=None):
    e = {"name": name, "time": t, "enabled": enabled}
    if prompt_path:
        e["prompt_path"] = prompt_path
    return e


def test_due_after_time_not_yet_fired():
    now = datetime(2026, 7, 8, 20, 31, tzinfo=TZ)
    due = schedule.due_duties([_entry()], now, {})
    assert [d["name"] for d in due] == ["review+plan"]


def test_not_due_before_time():
    now = datetime(2026, 7, 8, 20, 29, tzinfo=TZ)
    assert schedule.due_duties([_entry()], now, {}) == []


def test_fired_today_suppressed():
    now = datetime(2026, 7, 8, 20, 40, tzinfo=TZ)
    fired = {"review+plan": "2026-07-08"}
    assert schedule.due_duties([_entry()], now, fired) == []


def test_fired_yesterday_fires_again():
    now = datetime(2026, 7, 8, 20, 40, tzinfo=TZ)
    fired = {"review+plan": "2026-07-07"}
    assert len(schedule.due_duties([_entry()], now, fired)) == 1


def test_disabled_never_fires():
    now = datetime(2026, 7, 8, 21, 0, tzinfo=TZ)
    assert schedule.due_duties([_entry(enabled=False)], now, {}) == []


def test_prompt_path_passed_through():
    now = datetime(2026, 7, 8, 8, 5, tzinfo=TZ)
    due = schedule.due_duties(
        [_entry(name="wp", t="08:00", prompt_path="~/p.md")], now, {})
    assert due[0]["prompt_path"] == "~/p.md"


def test_bad_time_skipped():
    now = datetime(2026, 7, 8, 20, 40, tzinfo=TZ)
    assert schedule.due_duties([_entry(t="nope")], now, {}) == []


def test_schedule_trigger_pierces():
    now = datetime(2026, 7, 8, 20, 40, tzinfo=TZ)
    ctx = {"schedule": [{"name": "review+plan", "prompt_path": None}]}
    cfg = {"triggers": {"floor_min_min": 10, "floor_max_min": 55}}
    reasons = evaluate(ctx, cfg, now, next_floor_due_at=now + timedelta(hours=1))
    kinds = [r.kind for r in reasons]
    assert "schedule" in kinds
    # floor is not due -> only the piercing schedule fires
    assert "floor" not in kinds
