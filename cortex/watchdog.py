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

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from cortex import config, db, transcript, wake_state, window
from cortex.pacemaker import integration


def spawn(cfg: dict) -> int:
    """Launch a detached per-wake watchdog process (`python -m cortex.watchdog`)
    that outlives the pacemaker tick. Returns its pid."""
    log = wake_state.watchdog_pidfile_path(cfg).with_suffix(".log")
    log.parent.mkdir(parents=True, exist_ok=True)
    f = open(log, "a")
    p = subprocess.Popen(
        [sys.executable, "-m", "cortex.watchdog"],
        stdout=f, stderr=f, stdin=subprocess.DEVNULL,
        start_new_session=True, env={**os.environ},
    )
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


_DEFAULT_FUSE_PROMPT = (
    "⚙️ [FUSE] Summarise this whole session into one section and append it to "
    "handoff.md — follow the format and style of the preceding sections. Call "
    "lie_down(rotate=True) when done."
)


def _fuse(cfg: dict, grace: float) -> None:
    """Fuse path: esc the runaway turn, prompt the session to write its handoff
    and lie_down(rotate=True), then give it a bounded grace window to do so
    itself. If it lies down on its own within grace -> done. On timeout, or no
    reaction, fall back to the force path (SIGINT esc-equivalent + proxy
    lie_down); force_slept is set only when the handoff was NOT written this
    grace phase, so the catchup marker fires exactly when the handoff is missing.
    Hard deadline on the whole grace phase — the fuse must never hang."""
    from cortex import lie_down as lie_down_mod

    wcfg = cfg["wake"].get("watchdog", {})
    handoff_grace = float(wcfg.get("fuse_handoff_grace_sec", 300))
    prompt = wcfg.get("fuse_handoff_prompt") or _DEFAULT_FUSE_PROMPT

    window.send_esc(cfg)
    time.sleep(1.0)  # let esc land before typing the prompt
    handoff = config.handoff_path(cfg)
    before_mtime = handoff.stat().st_mtime if handoff.exists() else None
    window.inject_prompt(cfg, prompt)

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


def _free_round_note(cfg: dict) -> str:
    """Freshly rendered wakeup note for a free-round tuck-in (silence-gate OR
    wait-expiry — every free-round injection carries one, D6), or "" when the
    toggle is off / render fails. Diff mode: note.gather replays only events
    newer than the wake's last rendered note (wake_state.last_note_ts), so a
    second consecutive injection shows only what's new. Never raises — the
    tuck-in must land regardless."""
    if not cfg["wake"].get("wait_expiry_note", True):
        return ""
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
            data = note.gather(conn, cfg, now, window_sid=sid)
            return note.render(cfg, now, data).strip()
        finally:
            conn.close()
    except Exception:
        return ""


def _build_tuck_in_line(cfg: dict, mins: float) -> str:
    """Render the free-round line OUTSIDE any lock (BUG B: the slow note render +
    template fill must not run inside the strict section). {mins} = real minutes
    since the user's last message, {user} = marrow user_name. Every free-round
    injection (silence-gate AND wait-expiry, D6) prepends a freshly rendered
    (diff-mode) wakeup note ABOVE the 3-choice marker line — intel before choice
    (acceptance), and the marker lands LAST so it is the final decision cue. ""
    when disabled."""
    tmpl = str(cfg["wake"].get("tuck_in_text") or "").strip()
    if not tmpl:
        return ""
    line = tmpl.replace("{mins}", str(int(round(mins)))) \
               .replace("{user}", config.user_name(cfg))
    fresh = _free_round_note(cfg)
    if fresh:
        line = fresh + "\n" + line
    return line


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
    guard; these are the in-lock content invariants."""
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
        line = _build_tuck_in_line(cfg, silent_min) if allow_tuck else ""
        try:
            committed = wake_state.conditional_mutate(
                cfg, token0, _clear_wait_and_stamp())
        except wake_state.StateValidationError:
            return None  # stale epoch (user returned) / lock lost -> inject nothing
        if not committed:
            return None  # not awake / already stamped -> no double injection
        if allow_tuck:
            _write_tuck_in_line(cfg, line)
        return "wait-expiry free-round appended"

    if not wake_state.user_replied_this_wake(cfg):
        gate = float(wcfg.get("no_user_gate_min", 5))
        if silent_min >= gate:
            lie_down_mod.lie_down(cfg, force_slept="auto")
            return "no-user gate -> auto sleep"
        return None

    # Chat tier.
    silent_max = float(wcfg.get("silent_max_min", 15))
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
        line = _build_tuck_in_line(cfg, silent_min) if allow_tuck else ""
        try:
            committed = wake_state.conditional_mutate(
                cfg, token0, _stamp_tuck_pending())
        except wake_state.StateValidationError:
            return None  # slept / re-armed under us -> no tuck-in
        if not committed:
            return None  # awake cleared / wait live / already stamped
        if allow_tuck:
            _write_tuck_in_line(cfg, line)
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
