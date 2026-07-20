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


# --- ctl.cmd_wake remote control (manual take-office abolished) ---------------
#
# /ct-wake is a pure remote: the caller window NEVER takes office, never spawns,
# never writes cortex_claude_sid. The ONLY registration credential is a staged
# pending_claim written by the pacemaker spawn path (start_registration_handshake
# + the marrow claim), so no cmd_wake branch may ever write registration.
# Regression guard: a window with NO staged pending_claim never writes
# cortex_claude_sid, for ALL three branches (awake / dormant / dead).

def test_ctl_wake_live_awake_resident_reports_on_duty(cfg, monkeypatch):
    """Live resident + already awake -> on-duty text, zero side effects."""
    from cortex import ctl, wake
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign
    wake_state.set_awake(cfg, 1, None)
    spawned = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: spawned.__setitem__("n", spawned["n"] + 1))
    msg = ctl.cmd_wake(cfg)
    assert msg == cfg["wake"]["ctl_wake_awake_text"]
    assert spawned["n"] == 0  # awake -> nothing sent
    assert wake_state.is_awake(cfg) is True  # untouched


def test_ctl_wake_live_dormant_resident_signals(cfg, monkeypatch):
    """Live but dormant resident -> ear-signal wake now (run_wake), never takes
    office. Works whether the caller is foreign or the resident's own chain."""
    from cortex import ctl, wake
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)  # foreign
    fired = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    assert wake_state.is_awake(cfg) is False
    msg = ctl.cmd_wake(cfg)
    assert msg == cfg["wake"]["ctl_wake_signal_text"]
    assert fired["n"] == 1
    assert wake_state.is_awake(cfg) is False  # caller never takes office


# --- Regression: /ct-wake NEVER writes cortex_claude_sid (guard the write ------
# --- site at wake_state.py claim_office) for all three branches ---------------

def _assert_no_registration_write(cfg, staged_sid="foreign-caller"):
    st = wake_state.load(cfg)
    # a caller with NO staged pending_claim can never end up registered
    assert "pending_claim" not in st or st.get("cortex_claude_sid") != staged_sid


def test_ctl_wake_no_staged_claim_never_writes_registration_dead(cfg, monkeypatch):
    """Dead branch: no staged pending_claim -> cortex_claude_sid never written."""
    from cortex import ctl, wake
    monkeypatch.setattr(wake, "run_wake", lambda *a, **k: None)
    ctl.cmd_wake(cfg)  # no recorded resident -> dead branch
    assert "cortex_claude_sid" not in wake_state.load(cfg)


def test_ctl_wake_no_staged_claim_never_writes_registration_dormant(cfg, monkeypatch):
    """Dormant branch: no staged pending_claim -> cortex_claude_sid never written,
    even though a wake signal is sent."""
    from cortex import ctl, wake
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)
    monkeypatch.setattr(wake, "run_wake", lambda *a, **k: None)
    ctl.cmd_wake(cfg)
    # cmd_wake never CHANGES registration: the pre-existing resident sid stays,
    # no self-registration is written.
    assert wake_state.load(cfg).get("cortex_claude_sid") == "resident"


def test_ctl_wake_no_staged_claim_never_writes_registration_awake(cfg, monkeypatch):
    """Awake branch: on-duty text, cortex_claude_sid never written by the caller."""
    from cortex import ctl, wake
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: False)
    wake_state.set_awake(cfg, 1, None)
    monkeypatch.setattr(wake, "run_wake", lambda *a, **k: None)
    ctl.cmd_wake(cfg)
    assert wake_state.load(cfg).get("cortex_claude_sid") == "resident"


def test_ctl_wake_staged_pending_claim_never_promoted(cfg, monkeypatch):
    """Even WITH a staged pending_claim, cmd_wake never promotes it — manual
    take-office is abolished; only the spawn handshake claims registration."""
    from cortex import ctl, wake
    monkeypatch.setattr(wake, "run_wake", lambda *a, **k: None)
    wake_state.update(cfg, pending_claim={"sid": "manual-caller",
                                          "ts": "2026-01-01T00:00:00+00:00"})
    ctl.cmd_wake(cfg)  # no recorded resident -> dead branch, still no promote
    st = wake_state.load(cfg)
    assert st.get("cortex_claude_sid") != "manual-caller"


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


def test_claim_office_refuses_when_resident_pid_unresolved(cfg):
    """07-20 live incident, root cause: pgrep excludes its own ancestors, so a
    caller could never resolve its own claude pid -> resident_pid=None reached
    claim_office and used to silently GRANT with no pid recorded (fail-open
    inversion). Fixed: resident_pid=None is now a HARD refuse, audited
    distinctly (no_pid) — never a silent grant-without-a-pid."""
    ok = wake_state.claim_office(cfg, "ghost-sid", "test", resident_pid=None)
    assert ok is False
    d = wake_state.load(cfg)
    assert "cortex_claude_sid" not in d  # nothing written
    log = wake_state.config.wake_audit_log_path(cfg).read_text()
    assert "claim\trefuse" in log and "no_pid" in log


# NOTE: real-process-tree proof for claude_ancestor_pid (the `pgrep -a -x
# claude` fix) is NOT a pytest case — tests/conftest.py's _guarded() hard-blocks
# any real pgrep/ps/claude subprocess call in this suite (test isolation, by
# design: "no test may reach these"). Verified instead by direct manual
# invocation outside pytest (see P18 incident-fix report): resolved the live
# harness claude pid via the real pgrep/ps walk, confirmed comm=claude, and
# confirmed cmd_wake -> claim_office end-to-end against a scratch state dir
# records that real pid + writes the matching audit line.


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
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)
    monkeypatch.setattr("cortex.lie_down._chains_to_ancestor",
                        lambda pid, ancestor: True)  # in chain
    assert wake_state.resident_alive(cfg, caller_pid=100) is False


def test_resident_alive_rotated_sid_none_old_pid_alive_is_false(cfg, monkeypatch):
    """07-20 live bug 2 repro: rotate popped cortex_claude_sid (sid=None) but the
    OLD claude pid is still alive (user kept the retiring iTerm window open for
    scrollback) -> resident_alive must be False. A rotated/retired window is NOT
    a resident; office is registration (sid) + pid, not pid alone."""
    wake_state.update(cfg, rotated=True, cortex_resident_pid=33924)
    wake_state.update(cfg, retired_sid="old-session-sid")
    assert wake_state.load(cfg).get("cortex_claude_sid") is None
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)  # old pid alive
    assert wake_state.resident_alive(cfg) is False


def test_resident_alive_sid_set_pid_dead_is_false(cfg, monkeypatch):
    """Registered (sid set) but the recorded pid is dead -> not a resident."""
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="resident")
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: False)
    assert wake_state.resident_alive(cfg) is False


def test_resident_alive_no_sid_no_pid_is_false(cfg):
    """No registration and no recorded pid at all -> not a resident (the
    default/never-spawned state)."""
    assert wake_state.resident_alive(cfg) is False


def test_ctl_wake_rotated_sid_none_old_pid_alive_reports_not_on_duty(cfg, monkeypatch):
    """07-20 live bug 2 regression end-to-end: cmd_wake ran while state was
    rotated=True/cortex_claude_sid=None but the old resident pid was still alive
    -> must NOT report on-duty (the false positive that happened live). It falls
    into the no-resident branch and fires a respawn."""
    from cortex import ctl, wake
    wake_state.update(cfg, rotated=True, cortex_resident_pid=33924)
    monkeypatch.setattr(wake_state, "_pid_alive", lambda pid: True)  # old pid alive
    fired = {"n": 0}
    monkeypatch.setattr(wake, "run_wake",
                        lambda *a, **k: fired.__setitem__("n", fired["n"] + 1))
    line = ctl.cmd_wake(cfg)
    assert line != cfg["wake"]["ctl_wake_awake_text"]  # NOT reported on-duty
    assert fired["n"] == 1  # respawn fired instead


def test_spawn_registered_resident_two_ticks_no_respawn(cfg, monkeypatch):
    """THE regression (07-20 incident), now guaranteed by construction: a
    spawn-registered resident (cortex_claude_sid + recorded pid, alive) makes the
    reconcile tick see the ONE signal (resident_alive) and HOLD across two ticks —
    no respawn, registration untouched. A manual /ct-wake can no longer create
    such a registration at all (take-office abolished)."""
    wake_state.update(cfg, cortex_resident_pid=4321, cortex_claude_sid="spawn-win")
    wake_state.set_awake(cfg, 1, None)
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
    assert d2["cortex_claude_sid"] == "spawn-win"  # registration untouched
    assert d2["cortex_resident_pid"] == 4321


def test_accidental_close_aborts_if_resident_takes_office_mid_tick(cfg, monkeypatch):
    """TOCTOU: awake window, no ledger, no _window_alive -> would fire; but a live
    recorded resident (take-office between gate-eval and fire) aborts the respawn
    under the lock re-check."""
    wake_state.set_session_id(cfg, "SID-1")
    wake_state.update(cfg, awake=True, cortex_resident_pid=4321,
                      cortex_claude_sid="resident")
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
