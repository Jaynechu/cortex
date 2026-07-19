"""Phase 4 — resident singleton audit.

Invariant: exactly ONE live cortex resident at any time (0 and 2 are both
wrong), unless night-sleep or explicit pause/mute. These tests simulate the
three incident shapes with state-file fixtures and assert self-heal:
  - double-wake (2 residents): watchdog.spawn is idempotent (a live recorded
    pid = no second spawn); ctl.cmd_wake on an alive+awake window is a no-op.
  - watchdog-death-during-wait: the tick awake gate respawns a dead watchdog.
  - stale epoch: a superseded snapshot (gen moved) holds, no respawn/reap.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cortex import config, db, pacemaker_tick, wake_state, watchdog


@pytest.fixture
def cfg(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    c = config.load(path=tmp_path / "no-such.toml")
    c["paths"]["cortex_home"] = str(home)
    c["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    c["paths"]["self_schedule_file"] = str(home / "self_schedule.json")
    c["paths"]["transcript_dir"] = str(tmp_path / "transcript")
    c["paths"]["ny_db_pages"] = str(tmp_path / "ny")
    c["wake"]["sentinel"] = False
    return c


# --- _pid_alive ---------------------------------------------------------------

def test_pid_alive_self_true():
    import os
    assert watchdog._pid_alive(os.getpid()) is True


def test_pid_alive_none_and_dead_false():
    assert watchdog._pid_alive(None) is False
    assert watchdog._pid_alive(0) is False
    # a very high pid is almost certainly not a live process
    assert watchdog._pid_alive(4_000_000_000) is False


# --- spawn singleton guard (double-watchdog prevention) -----------------------

def test_spawn_skips_when_recorded_pid_alive(cfg, monkeypatch):
    """A live recorded watchdog pid whose IDENTITY confirms it is the cortex
    watchdog (ps command line names cortex.watchdog) = already on duty: spawn
    returns it, never launches a second subprocess."""
    import os
    wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))
    # Identity check (FIX 7): ps -p <pid> -o command= must name cortex.watchdog.
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **k: type("R", (), {
                            "returncode": 0,
                            "stdout": "python -m cortex.watchdog"})())
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 999})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 0  # no second watchdog
    assert pid == os.getpid()  # returns the live one


def test_spawn_launches_when_recorded_pid_recycled(cfg, monkeypatch):
    """FIX 7: a live recorded pid whose command line is NOT the watchdog (recycled
    pid inherited by an unrelated process) must NOT count as on-duty — spawn a
    fresh watchdog instead of heal-skipping forever."""
    import os
    wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))
    monkeypatch.setattr(watchdog.subprocess, "run",
                        lambda *a, **k: type("R", (), {
                            "returncode": 0,
                            "stdout": "/usr/bin/some-unrelated-process"})())
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4243})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1  # recycled pid ignored -> fresh watchdog spawned
    assert pid == 4243


def test_spawn_launches_when_no_record(cfg, monkeypatch):
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4242})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1
    assert pid == 4242


def test_concurrent_spawn_launches_only_one(cfg, monkeypatch):
    """FIX 2: the singleton check+spawn is serialised (flock) and the parent
    writes the pid claim before releasing — two concurrent callers can never both
    launch a watchdog. Simulate the race: two threads call spawn together; only
    ONE Popen fires, both return the same live pid."""
    import threading
    spawned = {"n": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_popen(*a, **k):
        with lock:
            spawned["n"] += 1
        return type("P", (), {"pid": 7777})()
    monkeypatch.setattr(watchdog.subprocess, "Popen", fake_popen)

    # Identity check reads the pidfile claim: alive+watchdog once a pid==7777 is
    # recorded (the parent writes it inside the lock before releasing).
    def fake_alive(cfg_, pid):
        return pid == 7777
    monkeypatch.setattr(watchdog, "_watchdog_pid_alive", fake_alive)

    results = {}

    def worker(idx):
        barrier.wait()
        results[idx] = watchdog.spawn(cfg)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert spawned["n"] == 1  # exactly one watchdog launched despite the race
    assert results[0] == results[1] == 7777  # both callers got the live pid


def test_spawn_launches_when_recorded_pid_dead(cfg, monkeypatch):
    wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead pid
    spawned = {"n": 0}
    monkeypatch.setattr(watchdog.subprocess, "Popen",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1)
                        or type("P", (), {"pid": 4243})())
    pid = watchdog.spawn(cfg)
    assert spawned["n"] == 1  # dead record -> fresh spawn
    assert pid == 4243


# --- ctl.cmd_wake already-on-duty guard (double-wake prevention) --------------
#
# codex P0 (live-confirmed): rotated/retired_sid are NOT liveness signals —
# retired_sid is sticky forever once any rotate has ever happened, so a bare
# presence check made the refuse branch permanently dead code and let a
# SECOND window take office next to a genuinely live resident (three active
# cortex windows resulted). The only valid liveness signal is
# window.find_claude_pid + process-ancestry self-vs-foreign detection.

def test_ctl_wake_live_foreign_window_refuses_even_with_sticky_retired_sid(cfg, monkeypatch):
    """A live resident + a wake invoked from a FOREIGN window -> refuse, zero
    side effects, one resident — regardless of how sticky/stale retired_sid is."""
    from cortex import ctl, window
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: None)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign: no ancestry match
    wake_state.set_retired_sid(cfg, "sticky-from-a-past-rotate")  # must not grant
    wake_state.set_awake(cfg, 1, None)
    msg = ctl.cmd_wake(cfg)
    assert msg == cfg["wake"]["ctl_wake_resident_text"]
    assert wake_state.is_awake(cfg) is True  # untouched -> still exactly one resident


def test_ctl_wake_live_dormant_foreign_window_also_refuses(cfg, monkeypatch):
    """Alive-but-dormant (not awake) resident, woken from a FOREIGN window,
    still refuses — dormant-ness never overrides the foreign-window check."""
    from cortex import ctl, window
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: None)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign
    assert wake_state.is_awake(cfg) is False
    msg = ctl.cmd_wake(cfg)
    assert msg == cfg["wake"]["ctl_wake_resident_text"]
    assert wake_state.is_awake(cfg) is False  # zero side effects


def test_ctl_wake_live_dormant_self_window_grants(cfg, monkeypatch):
    """Alive-but-dormant resident, woken from INSIDE that same window (self
    re-wake) -> granted, exactly one resident afterward."""
    from cortex import ctl, window
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: 4321)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: ancestor == 4321)  # self
    assert wake_state.is_awake(cfg) is False
    ctl.cmd_wake(cfg)
    assert wake_state.is_awake(cfg) is True


# --- P17 gap fix: stage-then-promote registration (refused wake leaves the ----
# --- true resident's cortex_claude_sid + identity fully untouched) -----------

def test_ctl_wake_refused_foreign_wake_leaves_registration_untouched(cfg, monkeypatch):
    """A foreign window's staged pending_claim must be DISCARDED on refusal,
    never promoted — the true resident's cortex_claude_sid (and therefore
    marrow's is_cortex_session identity for that resident) stays exactly as it
    was before the foreign /ct-wake ran."""
    from cortex import ctl, window
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: None)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign
    wake_state.update(cfg, cortex_claude_sid="true-resident",
                      pending_claim={"sid": "foreign-window", "ts": "2026-01-01T00:00:00+00:00"})
    msg = ctl.cmd_wake(cfg)
    assert msg == cfg["wake"]["ctl_wake_resident_text"]
    st = wake_state.load(cfg)
    assert st["cortex_claude_sid"] == "true-resident"  # untouched
    assert "pending_claim" not in st  # discarded, not left dangling


def test_ctl_wake_granted_promotes_staged_claim_to_registration(cfg, monkeypatch):
    """No live resident (dead/none) -> take office AND promote the staged
    pending_claim to cortex_claude_sid, recording this window's own claude pid."""
    from cortex import ctl, window
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: 5555)
    wake_state.update(cfg, pending_claim={"sid": "new-window", "ts": "2026-01-01T00:00:00+00:00"})
    ctl.cmd_wake(cfg)
    st = wake_state.load(cfg)
    assert st["cortex_claude_sid"] == "new-window"
    assert st["cortex_resident_pid"] == 5555  # own claude pid recorded
    assert "pending_claim" not in st


def test_ctl_wake_self_rewake_promotes_idempotently(cfg, monkeypatch):
    """Self re-wake of a dormant resident: promote is idempotent — same sid
    staged as already registered still lands cleanly."""
    from cortex import ctl, window
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: 4321)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: ancestor == 4321)  # self
    wake_state.update(cfg, cortex_claude_sid="dormant-self",
                      pending_claim={"sid": "dormant-self", "ts": "2026-01-01T00:00:00+00:00"})
    ctl.cmd_wake(cfg)
    st = wake_state.load(cfg)
    assert st["cortex_claude_sid"] == "dormant-self"
    assert "pending_claim" not in st


def test_ctl_wake_no_pending_claim_never_crashes_registration_unchanged(cfg, monkeypatch):
    """Item 3 fallback: ctl wake with NO staged claim AND no transcript sid to
    fall back on -> take-office still proceeds, registration left as-is (no
    crash, no spurious registration write)."""
    from cortex import ctl, transcript, window
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: None)
    monkeypatch.setattr(transcript, "newest", lambda c: None)  # no transcript sid
    wake_state.update(cfg, cortex_claude_sid="whatever-was-there")
    ctl.cmd_wake(cfg)
    st = wake_state.load(cfg)
    assert st["cortex_claude_sid"] == "whatever-was-there"  # unchanged
    assert "pending_claim" not in st


# --- awake-gate watchdog-liveness heal (watchdog death during a wait) ---------

def _awake_window(cfg, conn):
    conn.execute(
        "INSERT INTO ct_wake_log (ts, wake, dry_run, explanation) VALUES (?,1,0,?)",
        (db.utcnow_iso(), "w"))
    conn.commit()
    wid = conn.execute("SELECT MAX(id) AS id FROM ct_wake_log").fetchone()["id"]
    wake_state.set_awake(cfg, wid, None)
    return wid


def test_awake_gate_respawns_dead_watchdog(cfg, monkeypatch):
    """Watchdog died mid-wake: the 5-min tick awake gate must respawn one so the
    resident regains 60s polling + exact-time fuse."""
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        snap_gen = st.get("gen")
        wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead
        monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
        monkeypatch.setattr(watchdog, "silence_action", lambda *a, **k: None)
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        pacemaker_tick._handle_awake(conn, cfg, st, snap_gen=snap_gen)
        assert respawned["n"] == 1  # dead watchdog respawned
    finally:
        conn.close()


def test_awake_gate_no_respawn_when_watchdog_alive(cfg, monkeypatch):
    import os
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        snap_gen = st.get("gen")
        wake_state.watchdog_pidfile_path(cfg).write_text(str(os.getpid()))  # alive
        monkeypatch.setattr("cortex.wake._window_alive", lambda c: True)
        monkeypatch.setattr(watchdog, "silence_action", lambda *a, **k: None)
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        pacemaker_tick._handle_awake(conn, cfg, st, snap_gen=snap_gen)
        assert respawned["n"] == 0  # live watchdog -> no second spawn
    finally:
        conn.close()


def test_awake_gate_stale_epoch_holds_no_respawn(cfg, monkeypatch):
    """Stale snapshot (gen moved since the tick opened = a user reset / lie_down):
    the awake gate must hold and NOT respawn a watchdog against a dead epoch."""
    conn = db.connect(cfg)
    try:
        _awake_window(cfg, conn)
        st = wake_state.load(cfg)
        stale_gen = (st.get("gen") or 0) - 1  # snapshot older than live
        wake_state.watchdog_pidfile_path(cfg).write_text("4000000000")  # dead
        respawned = {"n": 0}
        monkeypatch.setattr(watchdog, "spawn",
                            lambda c: respawned.__setitem__("n", respawned["n"] + 1))
        msg = pacemaker_tick._handle_awake(conn, cfg, st, snap_gen=stale_gen)
        assert "superseded" in msg
        assert respawned["n"] == 0  # never respawn against a stale epoch
    finally:
        conn.close()


# --- P18: single registration gate (claim_office / resident_alive) ------------

def test_claim_office_grant_no_resident_records_pid(cfg):
    """No recorded resident -> grant: writes sid + resident pid, audits grant."""
    ok = wake_state.claim_office(cfg, "sid-a", "test", resident_pid=1111)
    assert ok is True
    d = wake_state.load(cfg)
    assert d["cortex_claude_sid"] == "sid-a"
    assert d["cortex_resident_pid"] == 1111
    log = wake_state.config.wake_audit_log_path(cfg).read_text()
    assert "claim\tgrant" in log and "via=test" in log


def test_claim_office_refuse_live_foreign_resident_zero_write(cfg, monkeypatch):
    """Recorded resident pid alive + NOT in caller chain -> refuse, zero write,
    audit refuse line present."""
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)
    ok = wake_state.claim_office(cfg, "intruder", "test", resident_pid=9999)
    assert ok is False
    d = wake_state.load(cfg)
    assert d["cortex_claude_sid"] == "resident"  # untouched
    assert d["cortex_resident_pid"] == 4321
    assert "claim\trefuse" in wake_state.config.wake_audit_log_path(cfg).read_text()


def test_claim_office_force_overrides_live_resident_audits_force(cfg, monkeypatch):
    """--force skips the refuse verdict but still routes through claim_office and
    audits via=force."""
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)
    ok = wake_state.claim_office(cfg, "override", "force", resident_pid=8888, force=True)
    assert ok is True
    d = wake_state.load(cfg)
    assert d["cortex_claude_sid"] == "override"
    assert d["cortex_resident_pid"] == 8888
    log = wake_state.config.wake_audit_log_path(cfg).read_text()
    assert "claim\tgrant" in log and "via=force" in log


def test_claim_office_dead_resident_is_claimable(cfg, monkeypatch):
    """A dead recorded resident (kill -0 fails) = office claimable (retired_sid /
    clean-vs-dirty is irrelevant here — dead means claimable)."""
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="dead-one")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: False)
    ok = wake_state.claim_office(cfg, "fresh", "test", resident_pid=2222)
    assert ok is True
    assert wake_state.load(cfg)["cortex_claude_sid"] == "fresh"


def test_resident_alive_self_chain_not_foreign(cfg, monkeypatch):
    """Recorded pid alive but in the caller's own chain = self re-wake (False,
    grantable), not a foreign holder."""
    wake_state.update(cfg, cortex_resident_pid=4321)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: True)  # in chain
    assert wake_state.resident_alive(cfg, caller_pid=100) is False


def test_manual_take_office_then_two_ticks_no_respawn(cfg, monkeypatch):
    """THE regression (07-20 incident): a manual /ct-wake take-office records a
    live resident; two reconcile ticks then see the ONE signal (resident_alive)
    and HOLD — no respawn, registration untouched."""
    from cortex import ctl, window
    # manual take-office: no live resident, this window becomes it (pid 4321).
    monkeypatch.setattr(window, "claude_ancestor_pid", lambda p=None: 4321)
    wake_state.update(cfg, pending_claim={"sid": "manual-win",
                                          "ts": "2026-01-01T00:00:00+00:00"})
    ctl.cmd_wake(cfg)
    d = wake_state.load(cfg)
    assert d["cortex_claude_sid"] == "manual-win"
    assert d["cortex_resident_pid"] == 4321
    assert d["awake"] is True
    # the recorded resident pid is alive + foreign to the tick (no chain).
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)  # hand-opened
    fired = {"n": 0}
    monkeypatch.setattr(pacemaker_tick, "_fire_dead_window",
                        lambda conn, c, why: fired.__setitem__("n", fired["n"] + 1))
    now = datetime.now(timezone.utc)
    for _ in range(2):
        st = wake_state.load(cfg)
        pacemaker_tick._reconcile(None, cfg, st, now)
    assert fired["n"] == 0  # NO respawn across two ticks
    d2 = wake_state.load(cfg)
    assert d2["cortex_claude_sid"] == "manual-win"  # registration untouched
    assert d2["cortex_resident_pid"] == 4321


def test_accidental_close_aborts_if_resident_takes_office_mid_tick(cfg, monkeypatch):
    """TOCTOU: awake window, no ledger, no _window_alive -> would fire; but a live
    recorded resident (take-office between gate-eval and fire) aborts the respawn
    under the lock re-check."""
    wake_state.set_session_id(cfg, "SID-1")
    wake_state.update(cfg, awake=True, cortex_resident_pid=4321)
    monkeypatch.setattr("cortex.wake._window_alive", lambda c: False)
    # resident_alive: first the top-of-reconcile OR check must NOT hold (else it
    # returns None before the branch); force it foreign there but the under-lock
    # re-check also sees it -> abort. Simpler: recorded pid alive+foreign means the
    # top OR already holds. So test the lower re-check directly.
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)
    st = wake_state.load(cfg)
    snap_gen = st.get("gen")
    assert wake_state._resident_alive_under_lock(
        wake_state.load(cfg), 100) is True
    # the re-check gate returns False (abort) when a live foreign resident exists
    assert pacemaker_tick._accidental_close_still_valid(cfg, snap_gen) is False
