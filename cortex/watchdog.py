"""Per-wake watchdog: spawned at note injection, killed at lie_down, never
resident. Every poll_sec it reads the transcript (mtime + window tokens) and
the awake marker, applying two judgements:
  (b) silent past silent_max_min -> inject one free-round note + marker line and
      re-arm the same timer (perpetual cycle, never forces sleep). User replies
      keep the transcript mtime fresh, so an active conversation never elapses.
  (c) window tokens >= fuse -> esc, then prompt the session to write its
      handoff and lie_down(rotate=True), give it a bounded grace window
      (fuse_handoff_grace_sec) to do so itself, else force it down (fuse).
On the fuse force path, esc is followed by a grace window (hard_interrupt_grace_sec):
if the transcript is still growing (esc didn't land, e.g. no focus), SIGINT the
resident claude process — a guaranteed esc-equivalent, at most once per trigger.
The catchup marker (force_slept) fires only when the handoff was NOT written.
Three-layer trace: esc/inject/SIGINT -> ct_wake_log.force_slept -> next note's
Last wake (watchdog.log carries the pid + skip/ambiguous detail).
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from cortex import config, db, occupancy, transcript, wake_state, window


def _pid_alive(pid: int | None) -> bool:
    """True if `pid` is a live process (signal 0 probe). None/invalid -> False.
    A PermissionError means the pid exists but is owned elsewhere = alive."""
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned elsewhere
    except (ValueError, TypeError, OverflowError):
        return False  # unparseable / out-of-range pid = treat as dead


def _recorded_watchdog_pid(cfg: dict) -> int | None:
    try:
        return int(wake_state.watchdog_pidfile_path(cfg).read_text().strip())
    except (OSError, ValueError):
        return None


# The watchdog runs as `python -m cortex.watchdog`; a recycled pid of an
# unrelated process would pass a bare kill(pid,0) liveness probe and make the
# singleton guard heal-skip forever. Confirm the pid's command line actually
# names this module before trusting the record.
_WATCHDOG_CMD_PATTERN = "cortex.watchdog"


def _watchdog_pid_alive(cfg: dict, pid: int | None) -> bool:
    """True only if `pid` is BOTH live AND actually the cortex watchdog process
    (identity check, not just kill(pid,0)). Guards the recycled-pid trap: after a
    reboot/pid wrap an unrelated process can inherit the recorded pid and read as
    "watchdog alive", so heals skip forever. macOS has no /proc — match the
    process command line via `ps -p <pid> -o command=` against the watchdog module
    pattern. If ps is unavailable/errors, fall back to bare liveness (better to
    risk a duplicate than to never heal). Dead pid -> False without spawning ps."""
    if not _pid_alive(pid):
        return False
    try:
        out = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return True  # cannot verify identity -> trust liveness (avoid dup spawn)
    if out.returncode != 0:
        return False  # ps found no such pid -> dead/recycled
    return _WATCHDOG_CMD_PATTERN in (out.stdout or "")


@contextlib.contextmanager
def _spawn_lock(cfg: dict):
    """Exclusive flock serialising the singleton check+spawn (BUG: the old
    check-then-act let two callers both pass the liveness test while the pidfile
    was absent/stale — the child writes its pid only AFTER Popen, so both parents
    spawned). Held across check + Popen + claim-write so the whole critical
    section is atomic. Advisory flock is released on ANY process exit (even a
    crash before writing the claim), so a caller that dies mid-spawn never
    deadlocks the next one — stale-claim recovery is just the next holder finding
    a dead/absent pid and spawning fresh."""
    lp = wake_state.watchdog_pidfile_path(cfg).with_suffix(".spawn.lock")
    lp.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def spawn(cfg: dict) -> int | None:
    """Launch a detached per-wake watchdog process (`python -m cortex.watchdog`)
    that outlives one daemon reconcile pass. Singleton guard (permanent-residency
    invariant): if the recorded pidfile still names a LIVE watchdog, do NOT spawn
    a second — return its pid. Only an absent/dead record spawns a fresh one. This
    is the single choke point behind every set_awake caller (fresh / typed bell)
    and marrow's _spawn_watchdog_if_absent, so a re-wake of an already-resident
    window never leaks a duplicate watchdog. Returns the live/new pid.

    Serialised under _spawn_lock: the check + Popen + claim-write run in one
    atomic critical section, so two concurrent callers can never both spawn. The
    parent writes the child pid into the pidfile immediately (the claim) before
    releasing the lock — no window where the next caller sees an empty pidfile for
    a watchdog that is in fact already spawning."""
    with _spawn_lock(cfg):
        existing = _recorded_watchdog_pid(cfg)
        if _watchdog_pid_alive(cfg, existing):
            return existing
        log = wake_state.watchdog_pidfile_path(cfg).with_suffix(".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        f = open(log, "a")
        p = subprocess.Popen(
            [sys.executable, "-m", "cortex.watchdog"],
            stdout=f, stderr=f, stdin=subprocess.DEVNULL,
            start_new_session=True, env={**os.environ},
        )
        # Parent writes the claim (child pid) before releasing the lock, so the
        # next caller sees a live pid instead of racing the child's own late
        # pidfile write in main().
        with contextlib.suppress(OSError):
            wake_state.watchdog_pidfile_path(cfg).write_text(str(p.pid))
        return p.pid


def _verify_esc_or_hard_interrupt(cfg: dict, grace_sec: float, trigger: str) -> str | None:
    """After an esc, poll the transcript mtime for up to grace_sec. If it's
    still growing (mid-generation, esc didn't land), SIGINT the resident
    claude process as a guaranteed fallback. Returns the pid string logged
    into the wake explanation, or None if esc alone was enough / disabled /
    discovery was ambiguous."""
    wcfg = cfg["wake"].get("watchdog", {})
    if not wcfg.get("hard_interrupt_enabled", True):
        return None
    before = transcript.mtime(cfg)
    if before is None:
        return None
    step = 2.0
    waited = 0.0
    while waited < grace_sec:
        time.sleep(min(step, grace_sec - waited))
        waited += step
        after = transcript.mtime(cfg)
        if after is None or after <= before:
            return None  # stopped growing -> esc landed, no hard interrupt needed
    pid = window.hard_interrupt(cfg)
    if pid is None:
        return f"hard-interrupt-skip:{trigger} (pid discovery ambiguous)"
    return f"hard-interrupt:{trigger} pid={pid}"


def _fuse(cfg: dict, grace: float) -> None:
    """Fuse path: esc the runaway turn, prompt the session to write its handoff
    and lie_down(rotate=True), then give it a bounded grace window to do so
    itself. If it lies down on its own within grace -> done. On timeout, or no
    reaction, fall back to the force path (SIGINT esc-equivalent + proxy
    lie_down); force_slept is set only when the handoff was NOT written this
    grace phase, so the catchup marker fires exactly when the handoff is missing.
    Hard deadline on the whole grace phase — the fuse must never hang.

    Covert delivery: only the "⚙️ [FUSE]" marker reaches the window (bell via the
    typed into the window). The full FUSE instruction body is
    injected invisibly by the marrow hook keyed on the marker ([cortex].fuse_prompt_text)."""
    from cortex import lie_down as lie_down_mod

    wcfg = cfg["wake"].get("watchdog", {})
    handoff_grace = float(wcfg.get("fuse_handoff_grace_sec", 300))
    marker_line = str(cfg["wake"].get("fuse_marker") or "⚙️ [FUSE]").strip()

    window.send_esc(cfg)
    time.sleep(1.0)  # let esc land before delivering the marker
    handoff = config.handoff_path(cfg)
    before_mtime = handoff.stat().st_mtime if handoff.exists() else None
    window.deliver_covert_marker(cfg, marker_line)

    # Poll for the session to lie down on its own (awake marker cleared) within
    # the grace window. Hard deadline = handoff_grace from now.
    deadline = time.time() + handoff_grace
    step = 3.0
    while time.time() < deadline:
        time.sleep(min(step, max(deadline - time.time(), 0.0)))
        if not wake_state.load(cfg).get("awake"):
            return  # session called lie_down itself -> done, catchup not needed

    # Timeout / no reaction. Did it at least write the handoff?
    written = _handoff_written(handoff, before_mtime)
    note = _verify_esc_or_hard_interrupt(cfg, grace, "fuse")
    reason = None if written else ("fuse" if not note else f"fuse {note}")
    lie_down_mod.lie_down(cfg, force_slept=reason)


def _handoff_written(handoff, before_mtime: float | None) -> bool:
    """True if the handoff file's mtime advanced (or it appeared) during the
    grace phase and it has content."""
    try:
        if not handoff.exists():
            return False
        st = handoff.stat()
        if before_mtime is not None and st.st_mtime <= before_mtime:
            return False
        return bool(handoff.read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _free_round_note(cfg: dict) -> tuple[str, str | None]:
    """Freshly rendered wakeup note for a free-round injection (silence-cycle OR
    kick carrier — every free-round injection carries one). Returns
    (text, pending_baseline_ts): text is "" when the toggle is off / render
    fails; pending_baseline_ts is the newest eligible replay ts that the caller
    must persist as the new diff baseline ONLY AFTER the line write + epoch
    commit succeed (FIX 6 — advancing it during render lost replay events forever
    when a stale-epoch / failed write dropped the injection). Diff mode: gather
    replays only events newer than the wake's last rendered note. Never raises —
    the injection must land regardless."""
    if not cfg["wake"].get("free_round_note", True):
        return "", None
    try:
        from datetime import datetime
        from pathlib import Path
        from zoneinfo import ZoneInfo
        from cortex import note

        tz = ZoneInfo(cfg.get("core", {}).get("timezone", "Australia/Melbourne"))
        now = datetime.now(tz)
        sid = None
        raw = wake_state.load(cfg).get("transcript")
        if raw:
            sid = Path(str(raw)).stem[:8]
        conn = db.connect(cfg)
        try:
            # advance_baseline=False: render must NOT persist the baseline. The
            # caller advances it only after the injection is committed.
            # claim_ct_notes=False (F9): a ct note must NOT be claimed at render
            # time — the render can run on a tick whose delivery is later dropped
            # (stale epoch) and would silently swallow the note. The caller claims
            # ct notes separately AFTER the delivery commits.
            # settle=True: this free-round render clears kick reasons / stamps
            # receipts (restoring the prior clear-at-render behavior — the line is
            # about to be typed into the window). ct notes stay deferred
            # (claim_ct_notes=False) and settle in _deliver_ct_notes AFTER the
            # delivery commits, so a dropped delivery never swallows a ct note.
            data = note.gather(conn, cfg, now, window_sid=sid,
                               advance_baseline=False, consume_kick=True,
                               claim_ct_notes=False, settle=True)
            text = note.render(cfg, now, data).strip()
            # FIX 6 + P2-B: the deferred advance must use the SAME cutoff this
            # note was built on, captured inside gather() — not a second query
            # here, which could race in an event this note never rendered and
            # then drop it when the baseline advances past it.
            pending = data.get("replay_cutoff_ts")
            # Mirror the FULL (non-diff) note to disk so a human reading the file
            # sees complete state — the injected note above stays diff-mode.
            # Best-effort: a mirror failure must not affect the tuck-in.
            try:
                from cortex import window
                full = note.gather(conn, cfg, now, window_sid=sid,
                                   advance_baseline=False, full_replay=True)
                window.write_note(cfg, note.render(cfg, now, full).strip())
            except Exception:
                pass
        finally:
            conn.close()
        return text, pending
    except Exception:
        return "", None


def _build_tuck_in_line(cfg: dict, mins: float) -> tuple[str, str | None]:
    """Render the free-round line OUTSIDE any lock (BUG B: the slow note render +
    template fill must not run inside the strict section). {mins} = real minutes
    since the user's last message, {user} = marrow user_name. Every free-round
    injection (silence-cycle AND kick carrier) prepends a freshly rendered
    (diff-mode) wakeup note ABOVE the marker line — intel before the marker
    (acceptance), and the marker lands LAST so it is the final cue.
    Returns (line, pending_baseline_ts): the caller advances the diff baseline to
    pending_baseline_ts ONLY AFTER the line is committed + written (FIX 6). ("",
    None) when disabled."""
    tmpl = str(cfg["wake"].get("tuck_in_text") or "").strip()
    if not tmpl:
        return "", None
    line = tmpl.replace("{mins}", str(int(round(mins)))) \
               .replace("{user}", config.user_name(cfg))
    fresh, pending = _free_round_note(cfg)
    if fresh:
        line = fresh + "\n" + line
    return line, pending


def _type_tuck_in_line(cfg: dict, line: str) -> bool:
    """Type a prebuilt tuck-in line into the resident window (one submitted
    turn). Same text as before, delivered by keystrokes instead of the retired
    ear. Returns True when the line landed (or was empty), False when there is no
    resident window / typing failed, so the atomic deliver+advance path skips the
    baseline advance on a failed delivery (no lost events)."""
    if not line:
        return True
    try:
        return bool(window.inject_prompt(cfg, line))
    except window.WindowError:
        return False


def _deliver_ct_notes(cfg: dict) -> None:
    """F9: after a free-round line has committed + landed, claim any pending ct
    notes and type them too — a real visible round is now guaranteed to surface
    them. Stamps claimed_by='cortex.free_round'. Done OUTSIDE the render (which
    passed claim_ct_notes=False) so an off-screen tick whose delivery was dropped
    never claims a note. Best-effort: never raises."""
    try:
        from cortex import db, note
        conn = db.connect(cfg)
        try:
            text = note.claim_ct_notes_text(cfg, conn, "cortex.free_round")
        finally:
            conn.close()
        if text:
            _type_tuck_in_line(cfg, text)
    except Exception:
        pass


def _stamp_free_round():
    """Mutator (run under conditional_mutate): stamp tuck_pending = now as the
    "last free-round injection" marker, unconditionally (used by both the
    silence-cycle and the kick-carrier fires — each successful injection re-arms
    from this instant, closed loop). Awake-only; a session that already slept
    under us must not be stamped. Returns True on stamp."""
    def _m(d: dict) -> bool:
        if not d.get("awake"):
            return False
        d["tuck_pending"] = datetime.now(timezone.utc).isoformat()
        return True
    return _m


def silence_action(cfg: dict, silent_min: float, *, allow_tuck: bool = True) -> str | None:
    """Perpetual free-round cycle, shared by the watchdog and the tick awake
    gate: every silent_max_min of user silence, inject one free-round note +
    marker line and re-arm the SAME timer from that instant — repeat forever,
    no forced sleep. tuck_pending doubles as the "last injection at"
    marker (renamed at the API surface only; the persisted field stays
    tuck_pending, T5 territory). When the user never spoke this wake,
    `silent_min` is timed from awake_since instead so the gate still elapses.
    An external kick carrier (kick.py mark_kick_round) short-circuits the
    silent_min gate and fires the injection immediately, then re-arms the same
    cycle. Returns an action label for logging, or None (keep waiting)."""
    wcfg = cfg["wake"].get("watchdog", {})
    # Capture the epoch at the START of the silence decision (BUG B): if a
    # lie_down / user reset bumps gen between here and the commit, the commit is
    # dropped so no line is appended after the session already slept.
    try:
        gen0, sid0 = wake_state.current_epoch(cfg)
        token0 = (gen0, sid0)
    except wake_state.StateValidationError:
        return None

    # Kick carrier (replaces the retired wait-expiry ride): an external kick
    # stamped kick_round=True to force this round's injection NOW, regardless of
    # silent_min. Consume it (read-and-clear) before deciding anything else so a
    # kick always gets exactly one carrier fire.
    if wake_state.peek_kick_round(cfg):
        line, pending_ts = ("", None)
        if allow_tuck:
            line, pending_ts = _build_tuck_in_line(cfg, silent_min)
        try:
            committed = wake_state.conditional_mutate(
                cfg, token0, _stamp_free_round())
        except wake_state.StateValidationError:
            return None  # stale epoch (user returned) / lock lost -> inject nothing
        if not committed:
            return None  # not awake -> no injection
        wake_state.take_kick_round(cfg)  # consume: exactly one carrier fire
        if allow_tuck:
            wake_state.deliver_then_advance(
                cfg, lambda: _type_tuck_in_line(cfg, line), pending_ts)
            _deliver_ct_notes(cfg)  # F9: claim ct notes now the round surfaces
        return "kick free-round appended"

    if not wake_state.user_replied_this_wake(cfg):
        # No user message this wake -> silent_min (derived from a user-message ts)
        # is 0.0 and would never elapse. Time from awake_since instead so the
        # same idle bar below still applies.
        elapsed = wake_state.awake_since_min(cfg)
        if elapsed is not None:
            silent_min = elapsed

    silent_max = float(wcfg.get("silent_max_min", 20))
    if silent_min < silent_max:
        return None
    st = wake_state.load(cfg)
    last_at = st.get("tuck_pending")
    if last_at is not None:
        # Already injected once this wake -> only re-fire once ANOTHER full
        # silent_max_min has elapsed since that injection (perpetual cycle, not
        # a re-fire on every poll past the threshold).
        try:
            marked = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            if marked.tzinfo is None:
                marked = marked.replace(tzinfo=timezone.utc)
            since_last = (datetime.now(timezone.utc) - marked).total_seconds() / 60.0
        except ValueError:
            since_last = silent_max  # unparseable marker -> treat as due
        if since_last < silent_max:
            return None

    # Build the (slow) tuck-in text OUTSIDE the lock, then commit atomically:
    # re-check awake + epoch under the strict lock and stamp the injection
    # marker in the same section (TOCTOU-safe). Only a committed stamp appends
    # the line.
    line, pending_ts = ("", None)
    if allow_tuck:
        line, pending_ts = _build_tuck_in_line(cfg, silent_min)
    try:
        committed = wake_state.conditional_mutate(
            cfg, token0, _stamp_free_round())
    except wake_state.StateValidationError:
        return None  # slept / re-armed under us -> no injection
    if not committed:
        return None  # awake cleared under us -> no injection
    if allow_tuck:
        wake_state.deliver_then_advance(
            cfg, lambda: _type_tuck_in_line(cfg, line), pending_ts)
        _deliver_ct_notes(cfg)  # F9: claim ct notes now the round surfaces
    return "free-round appended"


def _log(msg: str) -> None:
    """Timestamped heartbeat line to stdout (redirected to watchdog.log by the
    spawner). Proves the dedicated watchdog is alive vs riding only the tick
    backup — the log was silent for days with no way to tell a live watchdog
    from a dead one. Best-effort, never raises."""
    try:
        ts = datetime.now(timezone.utc).isoformat()
        print(f"{ts}\t{msg}", flush=True)
    except Exception:
        pass


def run(cfg: dict) -> int:
    wcfg = cfg["wake"].get("watchdog", {})
    poll = int(wcfg.get("poll_sec", 60))
    fuse = int(wcfg.get("fuse_tokens", 150_000))
    grace = float(wcfg.get("hard_interrupt_grace_sec", 30))

    _log(f"watchdog start pid={os.getpid()} poll={poll}s fuse={fuse}")
    while True:
        time.sleep(poll)
        st = wake_state.load(cfg)
        if not st.get("awake"):
            _log("awake cleared -> watchdog retires")
            return 0  # cortex lay down on its own -> watchdog retires
        # Window liveness gate: an accidentally-closed window leaves the ledger
        # awake, so silence_action / _fuse below would forge a proxy lie_down
        # (writing a future next_wake_at floor that starves the reconcile rescue
        # branch). A dead window must never receive a proxy sleep from here —
        # retire and let daemon reconcile (dead+awake+no-alarm -> resume) own
        # the revival.
        from cortex import wake
        if not wake._window_alive(cfg):
            _log("window dead -> no proxy sleep, watchdog retires; "
                 "reconcile owns revival")
            return 0
        if wake_state.is_paused(cfg):
            continue  # DND: no reaps / tuck-ins / fuse while paused

        # Silence source = minutes since the last REAL user message (assistant
        # turns / system writes / machine injections do NOT reset it). None (no user
        # message found in tail) -> 0.0 = hold, same as an unreadable transcript.
        silent_min = transcript.user_silent_min(cfg) or 0.0
        tokens = transcript.window_tokens(cfg)

        # Publish the live window occupancy (statusline total) for the next
        # wake's Budget line; reuse `tokens` computed above (also drives fuse).
        conn = db.connect(cfg)
        try:
            occupancy.store_window_tokens(conn, tokens)
        finally:
            conn.close()

        if fuse and tokens >= fuse:
            _log(f"fuse: tokens={tokens} >= {fuse}")
            _fuse(cfg, grace)
            return 0
        # Idle gate (free-round cycle), same bar regardless of user presence.
        # A proxy sleep here is force_slept="auto" (routine, not an incident).
        action = silence_action(cfg, silent_min)
        if action:
            _log(f"silence_action: {action} (silent={silent_min:.0f}min)")
            if not wake_state.load(cfg).get("awake"):
                return 0


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    if not config.shell_enabled(cfg):
        _log("cli shell off ([core].shells): watchdog not started")
        return 0
    pidfile = wake_state.watchdog_pidfile_path(cfg)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))
    try:
        return run(cfg)
    finally:
        try:
            if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()):
                pidfile.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
