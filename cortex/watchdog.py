"""Per-wake watchdog: spawned at note injection, killed at lie_down, never
resident. Every poll_sec it reads the transcript (mtime + window tokens) and
the awake marker, applying two judgements:
  (b) silent past silent_max_min without lie_down -> proxy lie_down (timeout).
      The routine end: user replies keep the transcript mtime fresh, so an
      active conversation never times out mid-turn.
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

from cortex import config, db, transcript, wake_state, window
from cortex.pacemaker import integration


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
    that outlives the pacemaker tick. Singleton guard (permanent-residency
    invariant): if the recorded pidfile still names a LIVE watchdog, do NOT spawn
    a second — return its pid. Only an absent/dead record spawns a fresh one. This
    is the single choke point behind every set_awake caller (fresh / ear / rearm)
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
    ear Monitor; typed only if the ear is dead). The full FUSE instruction body is
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


def _wait_until_live(cfg: dict) -> bool:
    """True if a one-shot silence window (cortex.wait) is still in the future.
    A live wait_until holds off every silence action (tuck-in / auto sleep)."""
    wu = wake_state.get_wait_until(cfg)
    return wu is not None and datetime.now(timezone.utc) < wu


def _free_round_note(cfg: dict) -> tuple[str, str | None]:
    """Freshly rendered wakeup note for a free-round tuck-in (silence-gate OR
    wait-expiry — every free-round injection carries one, D6). Returns
    (text, pending_baseline_ts): text is "" when the toggle is off / render
    fails; pending_baseline_ts is the newest eligible replay ts that the caller
    must persist as the new diff baseline ONLY AFTER the tuck-in write + epoch
    commit succeed (FIX 6 — advancing it during render lost replay events forever
    when a stale-epoch / failed write dropped the injection). Diff mode: gather
    replays only events newer than the wake's last rendered note. Never raises —
    the tuck-in must land regardless."""
    if not cfg["wake"].get("wait_expiry_note", True):
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
            data = note.gather(conn, cfg, now, window_sid=sid,
                               advance_baseline=False)
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
    injection (silence-gate AND wait-expiry, D6) prepends a freshly rendered
    (diff-mode) wakeup note ABOVE the 3-choice marker line — intel before choice
    (acceptance), and the marker lands LAST so it is the final decision cue.
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


def _advance_note_baseline(cfg: dict, pending_ts: str | None) -> None:
    """Persist the diff-mode replay baseline (wake_state.last_note_ts) to
    pending_ts, but ONLY after a free-round injection has actually committed +
    been written (FIX 6). Monotonic: only moves forward. A failed inject never
    reaches here, so the events it would have shown stay replayable next round.
    Best-effort — never raises."""
    if not pending_ts:
        return
    try:
        cur = wake_state.get_last_note_ts(cfg)
        if not cur or str(pending_ts) > str(cur):
            wake_state.set_last_note_ts(cfg, str(pending_ts))
    except Exception:
        pass


def _write_tuck_in_line(cfg: dict, line: str) -> None:
    """Append a prebuilt tuck-in line to wake_signal.log (the ear Monitor
    delivers it as a session turn). Byte-identical output to the old path."""
    if not line:
        return
    try:
        p = config.wake_signal_log_path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _wait_expired(cfg: dict) -> bool:
    """True when a wait(N) window was declared and its deadline is now in the PAST
    (exists but no longer live). Distinct from _wait_until_live: a future deadline
    holds silence; a past one triggers the free-round injection."""
    wu = wake_state.get_wait_until(cfg)
    return wu is not None and datetime.now(timezone.utc) >= wu


def _clear_wait_and_stamp():
    """Mutator (run under conditional_mutate): on a wait-expiry, atomically drop
    silence_wait_until AND stamp tuck_pending so the 5-min grace auto-lie arms.
    Stamps only when still awake + no tuck_pending yet. Returns True on a fresh
    stamp (caller appends the free-round line), False otherwise. The epoch check
    is conditional_mutate's token guard — a user message between expiry and poll
    (stale token) drops this whole branch (wait already cleared by the reset)."""
    def _m(d: dict) -> bool:
        if not d.get("awake"):
            return False
        d.pop("silence_wait_until", None)  # clear regardless (fires once)
        if d.get("tuck_pending") is not None:
            return False
        d["tuck_pending"] = datetime.now(timezone.utc).isoformat()
        return True
    return _m


def _stamp_tuck_pending():
    """Mutator (run under conditional_mutate): stamp tuck_pending ONLY if the
    session is still awake, has no live wait window, and no tuck_pending yet.
    Returns True when it stamped (caller then appends the line), False otherwise
    (nothing appended). The epoch check is done by conditional_mutate's token
    guard; these are the in-lock content invariants.

    Auto+manual share ONE wait quota per wake: this auto silence gate CONSUMES
    the wait counter when it stamps (bump wait_count to at least the cap), so a
    later manual wait() this wake is refused (menu only). No double-count when a
    wait already ran — only bump when still under cap."""
    def _m(d: dict) -> bool:
        if not d.get("awake"):
            return False
        if d.get("tuck_pending") is not None:
            return False
        raw = d.get("silence_wait_until")
        if raw is not None:
            try:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt > datetime.now(timezone.utc):
                    return False  # live wait -> hold, no tuck-in
            except ValueError:
                pass
        d["tuck_pending"] = datetime.now(timezone.utc).isoformat()
        try:
            used = int(d.get("wait_count", 0) or 0)
        except (TypeError, ValueError):
            used = 0
        d["wait_count"] = used + 1  # auto observe consumes one shared-quota slot
        return True
    return _m


def silence_action(cfg: dict, silent_min: float, *, allow_tuck: bool = True) -> str | None:
    """Two-tier silence decision, shared by the watchdog and the tick awake gate.

    Chat tier (user replied this wake): at silent_max_min, with no live
    wait_until, append the TUCK-IN marker once (tuck_pending stamped so it isn't
    re-appended); after tuck_grace_min more with no wait/lie_down -> proxy
    lie_down(auto).
    No-user tier (no reply this wake): at no_user_gate_min, no live wait_until
    -> proxy lie_down(auto) immediately, no marker.
    A live wait_until holds everything. Returns an action label for logging, or
    None (keep waiting / handled without sleeping)."""
    from cortex import lie_down as lie_down_mod

    wcfg = cfg["wake"].get("watchdog", {})
    # Capture the epoch at the START of the silence decision (BUG B): if a
    # lie_down / user reset bumps gen between here and the tuck-in commit, the
    # commit is dropped so no tuck-in is appended after the session already slept.
    try:
        gen0, sid0 = wake_state.current_epoch(cfg)
        token0 = (gen0, sid0)
    except wake_state.StateValidationError:
        return None
    if _wait_until_live(cfg):
        return None

    # Wait-expiry free-round (D1): a wait(N) window that has now elapsed injects
    # the free-round line IMMEDIATELY, bypassing the silent_min gate. Epoch-guarded
    # (BUG A): a user message between expiry and this poll bumps gen -> the token
    # is stale -> conditional_mutate raises and nothing is injected (the reset
    # already cleared the wait). On a fresh epoch: clear the wait + stamp
    # tuck_pending (grace arms) + append the free-round line.
    if _wait_expired(cfg):
        line, pending_ts = ("", None)
        if allow_tuck:
            line, pending_ts = _build_tuck_in_line(cfg, silent_min)
        try:
            committed = wake_state.conditional_mutate(
                cfg, token0, _clear_wait_and_stamp())
        except wake_state.StateValidationError:
            return None  # stale epoch (user returned) / lock lost -> inject nothing
        if not committed:
            return None  # not awake / already stamped -> no double injection
        if allow_tuck:
            _write_tuck_in_line(cfg, line)
            _advance_note_baseline(cfg, pending_ts)  # FIX 6: only after commit+write
        return "wait-expiry free-round appended"

    if not wake_state.user_replied_this_wake(cfg):
        # Accident safety net only (not a daily-flow step): fires when the user
        # never spoke this wake. Time it from awake_since (elapsed since wake) —
        # NOT from silent_min, which is derived from a user-message ts that on a
        # never-spoken wake is None -> 0.0 and would never elapse. Semantics
        # unchanged otherwise: proxy auto lie_down (arms next alarm via dice), no
        # marker.
        gate = float(wcfg.get("no_user_gate_min", 5))
        elapsed = wake_state.awake_since_min(cfg)
        if elapsed is None:
            elapsed = silent_min  # no awake_since -> fall back to prior behaviour
        if elapsed >= gate:
            lie_down_mod.lie_down(cfg, force_slept="auto")
            return "no-user gate -> auto sleep"
        return None

    # Chat tier.
    silent_max = float(wcfg.get("silent_max_min", 20))
    grace = float(wcfg.get("tuck_grace_min", 5))
    if silent_min < silent_max:
        return None
    st = wake_state.load(cfg)
    tuck_at = st.get("tuck_pending")
    if tuck_at is None:
        # Build the (slow) tuck-in text OUTSIDE the lock, then commit atomically:
        # re-check awake + epoch + no-live-wait + tuck_pending-still-absent under
        # the strict lock and stamp tuck_pending in the same section (fixes the
        # TOCTOU at the old :214-219). Only a committed stamp appends the line.
        line, pending_ts = ("", None)
        if allow_tuck:
            line, pending_ts = _build_tuck_in_line(cfg, silent_min)
        try:
            committed = wake_state.conditional_mutate(
                cfg, token0, _stamp_tuck_pending())
        except wake_state.StateValidationError:
            return None  # slept / re-armed under us -> no tuck-in
        if not committed:
            return None  # awake cleared / wait live / already stamped
        if allow_tuck:
            _write_tuck_in_line(cfg, line)
            _advance_note_baseline(cfg, pending_ts)  # FIX 6: only after commit+write
        return "tuck-in appended"
    # Marker already sent; wait out the grace window (measured from the marker).
    try:
        marked = datetime.fromisoformat(str(tuck_at).replace("Z", "+00:00"))
        if marked.tzinfo is None:
            marked = marked.replace(tzinfo=timezone.utc)
    except ValueError:
        marked = None
    grace_over = marked is None or (
        datetime.now(timezone.utc) - marked).total_seconds() / 60.0 >= grace
    if grace_over:
        lie_down_mod.lie_down(cfg, force_slept="auto")
        return "tuck grace elapsed -> auto sleep"
    return None


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
        if wake_state.is_paused(cfg):
            continue  # DND: no reaps / tuck-ins / fuse while paused

        # Silence source = minutes since the last REAL user message (assistant
        # turns / system writes / ear injections do NOT reset it). None (no user
        # message found in tail) -> 0.0 = hold, same as an unreadable transcript.
        silent_min = transcript.user_silent_min(cfg) or 0.0
        tokens = transcript.window_tokens(cfg)

        # Publish the live window occupancy (statusline total) for the next
        # wake's Budget line; reuse `tokens` computed above (also drives fuse).
        conn = db.connect(cfg)
        try:
            integration.store_window_tokens(conn, tokens)
        finally:
            conn.close()

        if fuse and tokens >= fuse:
            _log(f"fuse: tokens={tokens} >= {fuse}")
            _fuse(cfg, grace)
            return 0
        # Two-tier silence: chat (tuck-in then grace) / no-user (short gate).
        # A proxy sleep here is force_slept="auto" (routine, not an incident).
        action = silence_action(cfg, silent_min)
        if action:
            _log(f"silence_action: {action} (silent={silent_min:.0f}min)")
            if not wake_state.load(cfg).get("awake"):
                return 0


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
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
