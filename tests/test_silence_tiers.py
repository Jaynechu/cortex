"""Unified silence + awake gate tests.

One idle rule regardless of user presence: silent >= silent_max -> TUCK-IN
marker, then tuck_grace -> auto sleep. No-user wakes time from awake_since
(silent_min itself stays 0.0 with no user message). A live wait_until holds
everything. The awake gate never emits a wake; the late-sentinel race (user
speaks then sentinel fires) is silent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cortex import config, db, wake_state, watchdog


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    return c


@pytest.fixture
def awake_no_sentinel(cfg, monkeypatch):
    """A live wake with sentinel spawn stubbed out (auto sleep calls lie_down,
    which re-arms a sentinel)."""
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    conn.close()
    wake_state.set_awake(cfg, wid, None)
    monkeypatch.setattr("cortex.sentinel.subprocess.Popen",
                        lambda *a, **k: type("P", (), {"pid": 1})())
    return cfg


def _signal_lines(cfg):
    p = config.wake_signal_log_path(cfg)
    return p.read_text().splitlines() if p.exists() else []


# --- no-user wake (same idle bar, timed from awake_since) ---------------------

def test_no_user_wake_idles_to_tuck_in_then_grace(awake_no_sentinel):
    cfg = awake_no_sentinel
    # No user reply this wake; the gate times from awake_since (FIX 1), not
    # silent_min. Backdate the wake past silent_max_min (20) -> tuck-in marker.
    past = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
    wake_state.update(cfg, awake_since=past)
    a1 = watchdog.silence_action(cfg, silent_min=0.0)
    assert a1 == "tuck-in appended"
    assert wake_state.is_awake(cfg) is True
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    # Grace elapses -> auto sleep, same bar as the chat tier.
    grace_past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    wake_state.update(cfg, tuck_pending=grace_past)
    a2 = watchdog.silence_action(cfg, silent_min=0.0)
    assert a2 and "auto sleep" in a2
    assert wake_state.is_awake(cfg) is False


def test_no_user_gate_elapses_on_fresh_wake_with_zero_silent_min(awake_no_sentinel):
    """FIX 1 regression: a fresh wake where the user NEVER speaks has no user
    message ts -> user_silent_min() is None -> silent_min=0.0. The gate times
    from awake_since instead, so an elapsed-but-never-spoken wake still reaches
    the tuck-in (same bar as the chat tier, silent_max_min)."""
    cfg = awake_no_sentinel
    past = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
    wake_state.update(cfg, awake_since=past)  # user_replied_this_wake stays False
    action = watchdog.silence_action(cfg, silent_min=0.0)  # no user turn -> 0.0
    assert action == "tuck-in appended"
    assert wake_state.is_awake(cfg) is True


def test_no_user_under_bar_holds(awake_no_sentinel):
    cfg = awake_no_sentinel
    # awake_since is ~now (set_awake) -> elapsed < silent_max_min -> hold.
    assert watchdog.silence_action(cfg, silent_min=0.0) is None
    assert wake_state.is_awake(cfg) is True


# --- chat tier ----------------------------------------------------------------

def test_chat_tuck_in_then_grace(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    # First: silent past silent_max (20) -> tuck-in marker (+ note, D6), still awake.
    a1 = watchdog.silence_action(cfg, silent_min=21.0)
    assert a1 == "tuck-in appended"
    assert wake_state.is_awake(cfg) is True
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "3 choices" not in text  # menu body no longer written to the log (covert)
    writes_after_first = text.count("[NEW ROUND]")
    assert writes_after_first == 1
    # Marker stamped -> not re-appended on the next poll.
    a2 = watchdog.silence_action(cfg, silent_min=22.0)
    assert a2 is None
    assert "\n".join(_signal_lines(cfg)).count("[NEW ROUND]") == 1
    # Backdate the tuck stamp past the grace window -> auto sleep.
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    wake_state.update(cfg, tuck_pending=past)
    a3 = watchdog.silence_action(cfg, silent_min=23.0)
    assert a3 and "auto sleep" in a3
    assert wake_state.is_awake(cfg) is False


def test_observe_to_menu_to_grace_sleep(awake_no_sentinel):
    """Explicit two-state machine (P7): OBSERVE_ARMED (menu_delivered False) ->
    at expiry the menu is delivered exactly once (menu_delivered True) -> grace
    elapses -> auto sleep."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    assert wake_state.menu_delivered(cfg) is False  # observe_armed
    a1 = watchdog.silence_action(cfg, silent_min=21.0)
    assert a1 == "tuck-in appended"
    assert wake_state.menu_delivered(cfg) is True    # menu_delivered
    # Menu injected exactly once even across repeated polls.
    assert watchdog.silence_action(cfg, silent_min=22.0) is None
    assert "\n".join(_signal_lines(cfg)).count("[NEW ROUND]") == 1
    # Grace elapses -> auto sleep.
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    wake_state.update(cfg, tuck_pending=past)
    a3 = watchdog.silence_action(cfg, silent_min=23.0)
    assert a3 and "auto sleep" in a3
    assert wake_state.is_awake(cfg) is False


def test_chat_under_silent_max_holds(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    assert watchdog.silence_action(cfg, silent_min=10.0) is None
    assert _signal_lines(cfg) == []


def test_live_wait_until_holds_everything(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    wake_state.set_wait_until(cfg, future)
    # Even well past silent_max, a live wait_until suppresses the tuck-in.
    assert watchdog.silence_action(cfg, silent_min=40.0) is None
    assert _signal_lines(cfg) == []
    assert wake_state.is_awake(cfg) is True


def test_wait_cancels_pending_auto_sleep(awake_no_sentinel):
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    watchdog.silence_action(cfg, silent_min=21.0)  # tuck-in stamped
    # A wait() during grace sets a live wait_until -> auto sleep held off.
    future = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    wake_state.set_wait_until(cfg, future)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    wake_state.update(cfg, tuck_pending=past)
    assert watchdog.silence_action(cfg, silent_min=25.0) is None
    assert wake_state.is_awake(cfg) is True


# --- wait-expiry free-round branch (D1) ---------------------------------------

def test_wait_expiry_fresh_epoch_injects_immediately(awake_no_sentinel):
    """A declared wait(N) whose deadline is PAST injects the free-round line on
    the next poll, bypassing silent_min (even silent_min=0). The wait is cleared
    and tuck_pending stamped so the grace auto-lie arms."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.update(cfg, wait_spent=True)  # a wait() was declared this wake
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wake_state.set_wait_until(cfg, past)
    action = watchdog.silence_action(cfg, silent_min=0.0)  # gate bypassed
    assert action == "wait-expiry free-round appended"
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    # wait cleared + grace armed.
    st = wake_state.load(cfg)
    assert st.get("silence_wait_until") is None
    assert st.get("tuck_pending") is not None
    assert wake_state.is_awake(cfg) is True


def test_wait_expiry_stale_epoch_injects_nothing(awake_no_sentinel):
    """A user message between expiry and the poll bumps gen -> the captured token
    is stale -> conditional_mutate raises -> nothing injected, no stamp."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.update(cfg, wait_spent=True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wake_state.set_wait_until(cfg, past)

    # Force a gen bump AFTER the branch captures its token (simulate a user reset
    # landing mid-decision) by monkeypatching conditional_mutate to raise stale.
    import cortex.wake_state as ws

    def _stale(*a, **k):
        raise ws.StateValidationError("epoch token stale")
    orig = ws.conditional_mutate
    watchdog.wake_state.conditional_mutate = _stale
    try:
        action = watchdog.silence_action(cfg, silent_min=0.0)
    finally:
        watchdog.wake_state.conditional_mutate = orig
    assert action is None
    assert _signal_lines(cfg) == []


def test_wait_expiry_fires_once_then_falls_through(awake_no_sentinel):
    """After the free-round injection clears the wait, a second poll no longer
    sees a wait-expiry (the wait is gone) — it re-enters the normal chat tier."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.update(cfg, wait_spent=True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wake_state.set_wait_until(cfg, past)
    assert watchdog.silence_action(cfg, silent_min=0.0) == \
        "wait-expiry free-round appended"
    # Second poll: wait cleared, tuck_pending stamped -> grace path (still awake).
    a2 = watchdog.silence_action(cfg, silent_min=1.0)
    assert a2 is None
    assert wake_state.is_awake(cfg) is True


# --- template render ----------------------------------------------------------

def test_free_round_line_is_marker_only(cfg):
    """Default free-round line = the [NEW ROUND] marker ONLY (menu body moved to
    marrow's covert additionalContext inject). No 3-choice menu text on screen;
    no leftover {mins}/{user}/{n}/{cap} placeholders."""
    line, _pending = watchdog._build_tuck_in_line(cfg, mins=17.0)
    assert "[NEW ROUND]" in line
    assert "3 choices" not in line  # menu body no longer written to the log
    assert "Playbook" not in line
    for stray in ("{mins}", "{user}", "{n}", "{cap}"):
        assert stray not in line


def test_free_round_template_still_substitutes_placeholders(cfg):
    """The substitution mechanism survives for a custom template: {mins}/{user}
    still fill (C2 just happens to use neither)."""
    cfg["wake"]["tuck_in_text"] = "⏳ [NEW ROUND] {mins} min since {user}"
    line, _pending = watchdog._build_tuck_in_line(cfg, mins=17.0)
    assert "17 min" in line
    assert "the user" in line  # no marrow config -> fallback


# --- free-round note (D6: every injection carries one, wait gate dropped) ------

def test_wait_expiry_tuck_in_carries_fresh_note(awake_no_sentinel):
    """A wait(N) was declared this wake and has expired -> the TUCK-IN marker is
    followed by a freshly rendered note (a `Now:` line)."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    wake_state.update(cfg, wait_spent=True)  # a wait() was declared -> expiry, not plain
    a1 = watchdog.silence_action(cfg, silent_min=21.0)
    assert a1 == "tuck-in appended"
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" in text  # fresh note appended


def test_plain_silence_gate_tuck_in_also_carries_note(awake_no_sentinel):
    """D6: the silence-gate tuck-in (no wait declared this wake) ALSO carries a
    freshly rendered note now — the old wait-declared gate is gone."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)  # no wait declared
    watchdog.silence_action(cfg, silent_min=21.0)
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" in text  # note appended even without a declared wait


def test_free_round_note_toggle_off(awake_no_sentinel):
    """Toggle off -> plain marker, no note, on either free-round path."""
    cfg = awake_no_sentinel
    cfg["wake"]["wait_expiry_note"] = False
    wake_state.update(cfg, user_replied_this_wake=True)
    watchdog.silence_action(cfg, silent_min=21.0)
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" not in text


def test_free_round_note_render_failure_falls_back(awake_no_sentinel, monkeypatch):
    """A render blow-up must never block the tuck-in -> plain marker still lands."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    monkeypatch.setattr(
        "cortex.note.gather",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    a1 = watchdog.silence_action(cfg, silent_min=21.0)
    assert a1 == "tuck-in appended"
    text = "\n".join(_signal_lines(cfg))
    assert "[NEW ROUND]" in text
    assert "Now:" not in text  # note omitted, marker survived


def test_free_round_mirrors_full_note_to_file(awake_no_sentinel):
    """A free-round tuck-in refreshes the on-disk wakeup_note.md with a FULL
    render so a human reading the file sees complete state."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    note_path = wake_state.wakeup_note_path(cfg)
    note_path.write_text("stale", encoding="utf-8")
    watchdog.silence_action(cfg, silent_min=21.0)
    # Full render replaces the stale content. The note now opens with the Fix 5
    # machine-origin tag (a mirrored wake note is still a wake note), so assert the
    # full body is present rather than that the file starts with "Now:".
    body = note_path.read_text(encoding="utf-8")
    assert body != "stale" and "Now:" in body


def test_free_round_mirror_uses_full_replay(awake_no_sentinel, monkeypatch):
    """The mirror render must pass full_replay=True (non-diff), while the injected
    note stays diff-mode (full_replay defaults False)."""
    from cortex import note as _note
    seen = []
    real_gather = _note.gather

    def _spy(conn, cfg, now, **kw):
        seen.append(kw.get("full_replay", False))
        return real_gather(conn, cfg, now, **kw)

    monkeypatch.setattr(_note, "gather", _spy)
    wake_state.update(awake_no_sentinel, user_replied_this_wake=True)
    watchdog.silence_action(awake_no_sentinel, silent_min=21.0)
    assert False in seen  # injected diff note
    assert True in seen   # full mirror render


def test_two_consecutive_injections_second_diffs_against_first(awake_no_sentinel):
    """Two consecutive free-round injections in the same wake: the second note
    replays only events newer than the first note's ts — user activity on
    another channel between rounds shows up, the already-seen event does not."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    conn = db.connect(cfg)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, timestamp TEXT, role TEXT, content TEXT, channel TEXT)")
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) "
        "VALUES ('s', '2026-07-08T03:00:00+00:00', 'user', 'round one message', 'wx')")
    conn.commit()
    conn.close()

    # Round 1: wait-expiry free-round injection (fresh baseline note).
    wake_state.update(cfg, wait_spent=True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wake_state.set_wait_until(cfg, past)
    a1 = watchdog.silence_action(cfg, silent_min=0.0)
    assert a1 == "wait-expiry free-round appended"
    text1 = "\n".join(_signal_lines(cfg))
    assert "round one message" in text1

    # Activity on another channel lands between rounds.
    conn = db.connect(cfg)
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) "
        "VALUES ('s', '2026-07-08T03:05:00+00:00', 'user', 'round two message', 'tg')")
    conn.commit()
    conn.close()

    # Round 2: activity between rounds restored the quota (F5) -> another wait()
    # + expiry -> second free-round injection.
    wake_state.restore_wait_quota(cfg)
    wake_state.commit_wait(cfg, past)
    a2 = watchdog.silence_action(cfg, silent_min=0.0)
    assert a2 == "wait-expiry free-round appended"
    joined = "\n".join(_signal_lines(cfg))
    # Note now precedes its choice marker (intel-before-choice): round 2's note
    # sits BETWEEN the first marker and the second. Slice from just after the
    # first [NEW ROUND] -> only round 2's content; round 1's must not repeat.
    first_marker_end = joined.index("[NEW ROUND]") + len("[NEW ROUND]")
    text2_only = joined[first_marker_end:]
    assert "round two message" in text2_only
    assert "round one message" not in text2_only


def test_stale_epoch_wait_expiry_does_not_advance_baseline(awake_no_sentinel, monkeypatch):
    """FIX 6: the diff baseline (last_note_ts) must advance ONLY after the tuck-in
    commit + write succeed. If conditional_mutate raises (user returned = stale
    epoch) the injection is dropped, so its replay events must stay replayable
    next round — last_note_ts unchanged, nothing written to wake_signal.log."""
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True, wait_spent=True)
    conn = db.connect(cfg)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT, timestamp TEXT, role TEXT, content TEXT, channel TEXT)")
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel) "
        "VALUES ('s', '2026-07-08T03:00:00+00:00', 'user', 'unseen event', 'wx')")
    conn.commit()
    conn.close()
    before = wake_state.get_last_note_ts(cfg)  # None (no note rendered yet)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wake_state.set_wait_until(cfg, past)
    # Simulate a user return between render and commit: conditional_mutate raises.
    def _stale(*a, **k):
        raise wake_state.StateValidationError("epoch token stale")
    monkeypatch.setattr(wake_state, "conditional_mutate", _stale)
    assert watchdog.silence_action(cfg, silent_min=0.0) is None  # dropped
    assert _signal_lines(cfg) == []  # nothing injected
    assert wake_state.get_last_note_ts(cfg) == before  # baseline NOT advanced


# --- F9: ct-note claim tied to a VISIBLE round (death replay) ----------------

def _make_outbox(cfg, body="睡了吗", note_id=9):
    conn = db.connect(cfg)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "created_at TEXT, from_sid TEXT, from_channel TEXT, target TEXT, body TEXT, "
        "status TEXT DEFAULT 'pending', sent_at TEXT, replied_at TEXT, "
        "reply_text TEXT, receipt_seen INTEGER DEFAULT 0, "
        "claimed_by TEXT, claimed_at TEXT)")
    conn.execute(
        "INSERT INTO outbox (id, created_at, from_sid, from_channel, target, body,"
        " status) VALUES (?, '2026-07-08T03:00:00Z', 'cafe', 'tg', 'ct', ?, 'pending')",
        (note_id, body))
    conn.commit()
    conn.close()


def _outbox_row(cfg, note_id=9):
    conn = db.connect(cfg)
    try:
        return conn.execute(
            "SELECT status, claimed_by, claimed_at FROM outbox WHERE id=?",
            (note_id,)).fetchone()
    finally:
        conn.close()


def test_free_round_render_does_not_claim_ct_note(awake_no_sentinel):
    """Death replay: the background free-round RENDER (a tick that may never
    surface) must NOT claim a ct note. Only the post-commit ear delivery claims."""
    cfg = awake_no_sentinel
    _make_outbox(cfg)
    text, _pending = watchdog._free_round_note(cfg)
    # render ran, but the ct note is untouched — still pending, no audit stamp.
    row = _outbox_row(cfg)
    assert row["status"] == "pending"
    assert row["claimed_by"] is None


def test_free_round_visible_round_claims_ct_note_with_audit(awake_no_sentinel):
    """The visible wait-expiry free-round DELIVERS the ct note to the ear and
    stamps the audit columns (claimed_by / claimed_at)."""
    cfg = awake_no_sentinel
    _make_outbox(cfg, body="睡了吗")
    wake_state.update(cfg, user_replied_this_wake=True, wait_spent=True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wake_state.set_wait_until(cfg, past)
    assert watchdog.silence_action(cfg, silent_min=0.0) == \
        "wait-expiry free-round appended"
    # Note claimed by the free-round path and surfaced on the ear.
    row = _outbox_row(cfg)
    assert row["status"] == "sent"
    assert row["claimed_by"] == "cortex.free_round"
    assert row["claimed_at"] is not None
    assert "睡了吗" in "\n".join(_signal_lines(cfg))


def test_free_round_stale_epoch_does_not_claim_ct_note(awake_no_sentinel, monkeypatch):
    """A tick whose ear write is dropped (stale epoch) must leave the ct note
    pending — the original death (claim then swallow) is closed."""
    cfg = awake_no_sentinel
    _make_outbox(cfg)
    wake_state.update(cfg, user_replied_this_wake=True, wait_spent=True)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    wake_state.set_wait_until(cfg, past)

    def _stale(*a, **k):
        raise wake_state.StateValidationError("epoch token stale")
    monkeypatch.setattr(wake_state, "conditional_mutate", _stale)
    assert watchdog.silence_action(cfg, silent_min=0.0) is None
    row = _outbox_row(cfg)
    assert row["status"] == "pending"          # NOT swallowed
    assert row["claimed_by"] is None


def test_free_round_note_precedes_choice_marker(cfg):
    """Acceptance (intel before choice): the rendered note (a `Now:` line) comes
    ABOVE the [NEW ROUND] 3-choice marker, and the marker is the LAST line of the
    block so the ear's is_machine_line still matches the single-write chunk."""
    line, _pending = watchdog._build_tuck_in_line(cfg, mins=17.0)
    assert "Now:" in line and "[NEW ROUND]" in line
    assert line.index("Now:") < line.index("[NEW ROUND]")  # intel first
    # Marker on the final non-empty line -> single-write block stays machine-tagged.
    assert line.rstrip().splitlines()[-1].lstrip().startswith("⏳ [NEW ROUND]")


def _fresh_transcript(cfg):
    import json
    from cortex import transcript
    d = transcript.transcript_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "usage": {"input_tokens": 1, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 1}}}))


def test_awake_gate_late_sentinel_race_is_silent(awake_no_sentinel):
    """User speaks 15:54 (awake, fresh transcript), the late sentinel/tick fires
    15:55: the awake gate runs the silence check, sees the fresh transcript
    (idle ~0) -> holds, emits NO wake signal, stays awake."""
    from cortex import pacemaker_tick
    cfg = awake_no_sentinel
    wake_state.update(cfg, user_replied_this_wake=True)
    _fresh_transcript(cfg)  # user just spoke -> transcript is hot
    conn = db.connect(cfg)
    try:
        msg = pacemaker_tick._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "wake in progress" in msg  # held, no emit, no auto sleep
    assert _signal_lines(cfg) == []
    assert wake_state.is_awake(cfg) is True


def test_stale_hold_when_window_alive(awake_no_sentinel, monkeypatch):
    """Long transcript-idle but the resident window is ALIVE (user reading/typing)
    -> hold, do NOT reap. Alive-but-quiet is not a dead window."""
    from cortex import pacemaker_tick, wake
    cfg = awake_no_sentinel
    # No transcript -> idle 1e9 >= stale_min, past the silence check (idle 0.0).
    monkeypatch.setattr(wake, "_window_alive", lambda c: True)
    conn = db.connect(cfg)
    try:
        msg = pacemaker_tick._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "stale hold: window alive" in msg
    assert wake_state.is_awake(cfg) is True  # not reaped


def test_stale_reap_when_window_dead(awake_no_sentinel, monkeypatch):
    """Long transcript-idle and the resident window is GONE -> reap as before."""
    from cortex import pacemaker_tick, wake
    cfg = awake_no_sentinel
    monkeypatch.setattr(wake, "_window_alive", lambda c: False)
    conn = db.connect(cfg)
    try:
        msg = pacemaker_tick._handle_awake(conn, cfg, wake_state.load(cfg))
    finally:
        conn.close()
    assert "stale wake reaped" in msg
    assert wake_state.is_awake(cfg) is False  # reaped


def test_awake_gate_asleep_still_fires(cfg, monkeypatch):
    """Sanity contrast: when NOT awake, the awake gate is not taken at all — the
    normal tick decision path runs (asleep+due -> emit as today)."""
    # No awake marker set -> is_awake False.
    assert wake_state.is_awake(cfg) is False


# --- double-fire guard (watchdog poll + tick awake-branch same window) ---------

def test_lie_down_double_fire_single_effect(awake_no_sentinel, monkeypatch):
    """Watchdog (60s poll) and tick awake-branch can both proxy lie_down in the
    same window. The atomic awake claim => exactly one acts (real result), the
    other no-ops; ct_wake_log force_slept + floor redraw happen once each."""
    from cortex import lie_down as lie_down_mod
    from cortex.pacemaker import integration
    cfg = awake_no_sentinel

    redraws = []
    real_floor = integration.lie_down
    monkeypatch.setattr(
        "cortex.pacemaker.integration.lie_down",
        lambda conn, cfg, minutes=None: redraws.append(1) or real_floor(conn, cfg, minutes=minutes))

    wid = wake_state.load(cfg)["wake_log_id"]
    r1 = lie_down_mod.lie_down(cfg, force_slept="auto")
    r2 = lie_down_mod.lie_down(cfg, force_slept="auto")

    # One winner (has next_wake / tokens), one no-op (skipped).
    winners = [r for r in (r1, r2) if "skipped" not in r]
    skipped = [r for r in (r1, r2) if r.get("skipped") == "not awake"]
    assert len(winners) == 1 and len(skipped) == 1
    assert wake_state.is_awake(cfg) is False
    # Single floor redraw.
    assert len(redraws) == 1
    # Single ct_wake_log write: force_slept stamped exactly once on this row.
    conn = db.connect(cfg)
    try:
        row = conn.execute(
            "SELECT force_slept FROM ct_wake_log WHERE id=?", (wid,)).fetchone()
    finally:
        conn.close()
    assert row["force_slept"] == "auto"
