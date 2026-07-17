"""Persistent window/wake runtime state (JSON file, sibling of affect_flag /
self_schedule). Holds the resident iTerm session id, the awake marker
(awake_since + wake_log row id + transcript hint) and the rotate guard. Kept
out of the pure PacemakerState so the decision core stays I/O-free; all paths
resolve from config (OSS-overridable via [paths]).
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from cortex import config

_AWAKE_KEYS = ("awake", "awake_since", "wake_log_id", "transcript",
               "silence_wait_until", "wait_spent", "user_replied_this_wake",
               "tuck_pending", "last_note_ts")

_LOCK_TIMEOUT_SEC = 5.0


class StateValidationError(Exception):
    """Fail-closed sentinel: a strict-lock section could not acquire the lock,
    the state file was unreadable/malformed, or a captured (gen, state_id) token
    no longer matches the live state. Every deferred actor treats it as "abort
    the pending side effect silently" — correctness never depends on the lock
    succeeding, only that a doubtful mutation is dropped."""


def wake_state_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wake_state_file") or ""
    return Path(raw).expanduser() if raw else config.state_dir(cfg) / "wake_state.json"


def wakeup_note_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("wakeup_note_file") or ""
    return Path(raw).expanduser() if raw else config.cortex_home(cfg) / "wakeup_note.md"


def watchdog_pidfile_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("watchdog_pidfile") or ""
    return Path(raw).expanduser() if raw else config.state_dir(cfg) / "watchdog.pid"


def lock_path(cfg: dict) -> Path:
    """Sibling .lock file guarding load-modify-write. Shared byte-for-byte with
    the marrow hook side so cross-process updates never lose each other.
    COUPLED: base = [paths].wake_state_file / [paths].cortex_home. Marrow's side
    (cortex_bridge._wake_state_lock via _cortex_wake_state_path) resolves from
    marrow [cortex].wake_state_file / [cortex].home — override one without the
    other and the two lock files split (silent lost update)."""
    return wake_state_path(cfg).with_suffix(".lock")


@contextlib.contextmanager
def _flock(cfg: dict):
    """Blocking exclusive flock on the sibling .lock file (short timeout via a
    non-blocking retry loop). Best-effort: if the lock cannot be acquired the
    write still proceeds (an unlocked write is the pre-existing behaviour), so a
    lock-dir hiccup never wedges a wake."""
    lp = lock_path(cfg)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        yield
        return
    deadline = _mono() + _LOCK_TIMEOUT_SEC
    got = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if _mono() >= deadline:
                    break
                _sleep(0.02)
        yield
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


@contextlib.contextmanager
def _strict_flock(cfg: dict):
    """Fail-closed exclusive flock: unlike _flock (advisory, proceeds unlocked on
    timeout), this RAISES StateValidationError if the lock cannot be created or
    acquired within the timeout. Used for every consequential cancellation-epoch
    check + mutation, so a lock hiccup drops the doubtful side effect instead of
    racing an unlocked write."""
    lp = lock_path(cfg)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        raise StateValidationError(f"lock open failed: {e}") from e
    deadline = _mono() + _LOCK_TIMEOUT_SEC
    got = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if _mono() >= deadline:
                    raise StateValidationError("lock acquire timeout")
                _sleep(0.02)
        yield
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _mono() -> float:
    import time
    return time.monotonic()


def _sleep(sec: float) -> None:
    import time
    time.sleep(sec)


def _load_strict(cfg: dict) -> dict:
    """Read the state file, RAISING StateValidationError on any read/parse
    failure (unlike load() which returns {}). Caller must hold _strict_flock."""
    p = wake_state_path(cfg)
    try:
        if not p.exists():
            return {}
        return json.loads(p.read_text())
    except (OSError, ValueError) as e:
        raise StateValidationError(f"state unreadable/malformed: {e}") from e


def _ensure_epoch(d: dict) -> bool:
    """Initialise gen (0) + a random state_id on first touch. Returns True when a
    field was added (caller must persist). state_id defends the delete/recreate
    ABA: a fresh file re-seeds a different id, so a token captured against the
    old file never validates against the new one."""
    changed = False
    if not isinstance(d.get("gen"), int):
        d["gen"] = 0
        changed = True
    if not d.get("state_id"):
        d["state_id"] = secrets.token_hex(8)
        changed = True
    return changed


def current_epoch(cfg: dict) -> tuple[int, str]:
    """Capture the live (gen, state_id) token under the STRICT lock — a deferred
    actor's birth token. Raises StateValidationError on lock/parse failure so a
    doubtful capture never yields a token that would spuriously validate later."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        if _ensure_epoch(d):
            _save(cfg, d)
        return int(d["gen"]), str(d["state_id"])


def _token_current(d: dict, token: tuple[int, str] | None) -> bool:
    """True when a captured (gen, state_id) still matches the loaded state. A
    None token = legacy/no-token = always current (backward tolerance)."""
    if token is None:
        return True
    gen, state_id = token
    return isinstance(d.get("gen"), int) and d.get("gen") == gen \
        and str(d.get("state_id") or "") == str(state_id)


def token_current(cfg: dict, token: tuple[int, str] | None) -> bool:
    """Read-only epoch check under the STRICT lock: True if `token` still matches
    the live (gen, state_id). Raises StateValidationError on lock/parse failure
    (fail closed) so a deferred actor drops the side effect rather than proceed on
    a doubtful read. token=None -> True (legacy/no token)."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        _ensure_epoch(d)
        return _token_current(d, token)


def conditional_mutate(cfg: dict, token: tuple[int, str] | None, mutate):
    """Run `mutate(d)` and persist ONLY if `token` still matches the live epoch,
    all under the STRICT lock. `mutate` edits the dict in place; its return value
    is passed back to the caller. Raises StateValidationError on lock/parse
    failure OR token mismatch (fail closed) so the deferred side effect is
    dropped. token=None skips the check (unconditional, still strict-locked)."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        _ensure_epoch(d)
        if not _token_current(d, token):
            raise StateValidationError("epoch token stale")
        result = mutate(d)
        _save(cfg, d)
        return result


def bump_gen(cfg: dict) -> tuple[int, str]:
    """Increment gen under the strict lock and return the NEW (gen, state_id).
    The one primitive behind every cancellation epoch: a bump invalidates every
    token captured against the old gen. Callers that also mutate state should use
    the higher-level helpers (claim_lie_down, set_awake, wait, ...) which bump +
    mutate atomically in one locked section."""
    with _strict_flock(cfg):
        d = _load_strict(cfg)
        _ensure_epoch(d)
        d["gen"] = int(d["gen"]) + 1
        _save(cfg, d)
        return int(d["gen"]), str(d["state_id"])


def wake_audit(cfg: dict, action: str, reason: str = "", detail: str = "") -> None:
    """Append one tab-separated audit line (ISO-ts, action, reason, detail) to
    the config-routed wake-audit log. Byte-shared with marrow's _wake_audit.
    Best-effort — never raises."""
    try:
        path = config.wake_audit_log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        line = "\t".join((ts, action, str(reason).replace("\t", " "),
                          str(detail).replace("\t", " ")))
        with open(path, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load(cfg: dict) -> dict:
    p = wake_state_path(cfg)
    try:
        if p.exists():
            return json.loads(p.read_text())
    except (OSError, ValueError):
        pass
    return {}


# Legacy keys from older schema versions, dropped on the next _save so state
# files converge (nothing reads these anymore — verified in both repos).
_DEAD_KEYS = ("rotated_at",)


def _save(cfg: dict, data: dict) -> None:
    """Atomic whole-file write: temp file in the same dir + os.replace so a
    reader never sees a half-written file. Callers hold _flock for the
    read-modify-write; _save alone is atomic but not serialised."""
    p = wake_state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    for k in _DEAD_KEYS:
        data.pop(k, None)
    tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, p)


def update(cfg: dict, **kv) -> dict:
    with _flock(cfg):
        d = load(cfg)
        d.update(kv)
        _save(cfg, d)
        return d


def get_session_id(cfg: dict) -> str | None:
    return load(cfg).get("session_id")


def set_session_id(cfg: dict, sid: str) -> None:
    update(cfg, session_id=sid)


def is_awake(cfg: dict) -> bool:
    return bool(load(cfg).get("awake"))


def set_awake(cfg: dict, wake_log_id: int | None, transcript: str | None,
              expected_gen: int | None = None) -> tuple[int, str] | None:
    """Activate a wake (asleep -> awake). BUMPS gen (a fresh wake is a new epoch
    that invalidates the sleeping window's alarm token). When `expected_gen` is
    given the flip is CONDITIONAL: if the live gen has moved on (a newer lie_down
    / user reset re-armed since the wake decision), abort and return None. Returns
    the new (gen, state_id) on success, None if the conditional flip lost.

    next_wake_at is the durable ledger: a successful wake means it fired, so it is
    cleared here (re-armed by the next lie_down) in the same atomic section so an
    awake window never carries a stale scheduled time."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            if expected_gen is not None and int(d["gen"]) != int(expected_gen):
                return None
            d["gen"] = int(d["gen"]) + 1
            d.update(awake=True, next_wake_at=None,
                     awake_since=datetime.now(timezone.utc).isoformat(),
                     wake_log_id=wake_log_id, transcript=transcript,
                     wait_spent=False, user_replied_this_wake=False,
                     tuck_pending=None, last_note_ts=None)
            _save(cfg, d)
            return int(d["gen"]), str(d["state_id"])
    except StateValidationError:
        return None


def clear_awake(cfg: dict) -> None:
    """Clear the awake marker AND bump gen (a successful sleep is a new epoch —
    any alarm token from the just-ended wake is invalidated). Strict-locked."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            d["gen"] = int(d["gen"]) + 1
            for k in _AWAKE_KEYS:
                d.pop(k, None)
            _save(cfg, d)
    except StateValidationError:
        pass


def claim_lie_down(cfg: dict, force_slept: str | None = None) -> dict | None:
    """Atomic read-and-clear of the awake marker under the STRICT wake_state lock,
    so exactly one lie_down proceeds when the watchdog (60s poll) and the tick
    awake-branch both fire silence_action in the same window. On the winning claim
    (was awake -> now cleared) BUMPS gen — every deferred alarm from the ending
    wake is now stale, and the returned token is the NEW epoch the lie_down body
    carries through its late side effects. Returns the pre-clear snapshot PLUS a
    `claim_token` (gen, state_id) to the single winner; None to any later caller
    (already cleared / lock lost -> no-op, no bump). Writes a `lie_down_claim`
    audit line (old->new gen)."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            if not d.get("awake"):
                return None
            snapshot = dict(d)
            old_gen = int(d["gen"])
            d["gen"] = old_gen + 1
            new_gen = d["gen"]
            for k in _AWAKE_KEYS:
                d.pop(k, None)
            _save(cfg, d)
            snapshot["claim_token"] = (new_gen, str(d["state_id"]))
    except StateValidationError:
        return None
    wake_audit(cfg, "lie_down_claim", f"gen {old_gen}->{new_gen}",
               f"force_slept={force_slept}")
    return snapshot


# Observe/menu two-state machine (P7). The persisted `tuck_pending` field is the
# state carrier: absent/None = OBSERVE_ARMED (the auto silence gate or a declared
# wait is running, no menu shown yet); a stamped ISO ts = MENU_DELIVERED (the
# 3-choice menu was injected once at expiry, grace timer runs from that ts). The
# field name is kept for the epoch-guarded mutators + marrow's user-wake reset;
# these accessors name the states at the API surface.

def menu_delivered(cfg: dict) -> bool:
    """True once the expiry menu (C2) has been injected this wake (state =
    MENU_DELIVERED). False = OBSERVE_ARMED (still observing / holding a wait)."""
    return load(cfg).get("tuck_pending") is not None


def user_replied_this_wake(cfg: dict) -> bool:
    """True once a real user message landed in the current wake (set by the
    marrow UserPromptSubmit hook). Drives the chat vs no-user silence tier."""
    return bool(load(cfg).get("user_replied_this_wake"))


def awake_since_min(cfg: dict) -> float | None:
    """Minutes elapsed since this wake began (awake_since), or None when not
    awake / unparseable. The no-user silence tier times from HERE (elapsed since
    wake), not from a user-message ts that may never exist on a fresh wake where
    the user never spoke."""
    raw = load(cfg).get("awake_since")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def commit_wait(cfg: dict, until_iso: str) -> dict:
    """Accept one wait() as a single atomic strict-locked mutation: verify the
    session is still awake and the wait quota is not already spent (F5: blocks a
    CONSECUTIVE empty wait — any activity this round clears wait_spent first),
    BUMP gen (an accepted wait re-arms the silence window, invalidating the prior
    alarm token), set silence_wait_until, mark wait_spent, clear tuck_pending.
    Returns {"ok": bool, ...}. Never raises: a lock/parse failure returns
    ok=False, refused=True (fail closed — no half-applied wait)."""
    try:
        with _strict_flock(cfg):
            d = _load_strict(cfg)
            _ensure_epoch(d)
            if not d.get("awake"):
                return {"ok": False, "refused": True, "reason": "not awake"}
            if d.get("wait_spent"):
                return {"ok": False, "refused": True, "reason": "consecutive"}
            old_gen = int(d["gen"])
            d["gen"] = old_gen + 1
            new_gen = d["gen"]
            d["silence_wait_until"] = until_iso
            d["wait_spent"] = True
            d.pop("tuck_pending", None)
            _save(cfg, d)
    except StateValidationError:
        return {"ok": False, "refused": True, "reason": "state locked"}
    # Audit OUTSIDE the strict lock (parity with claim_lie_down): an accepted
    # wait bumps gen — a new cancellation epoch that must be visible in the
    # trail (a silent bump hid the wait during incident forensics).
    wake_audit(cfg, "commit_wait", f"gen {old_gen}->{new_gen}",
               f"until={until_iso}")
    return {"ok": True}


def set_wait_until(cfg: dict, until_iso: str) -> None:
    """Declare a one-shot silence window: the watchdog holds off its routine
    timeout lie-down until this UTC instant (the model is e.g. waiting for the
    user to come back). Cleared once the watchdog acts on it (take_wait_until)."""
    update(cfg, silence_wait_until=until_iso)


def get_wait_until(cfg: dict) -> datetime | None:
    """Peek the declared silence deadline (UTC-aware) or None — the watchdog
    reads this every poll: still-future = keep holding; past/absent = the
    routine silent_max_min threshold applies. Non-destructive; the watchdog
    calls clear_wait_until() once it acts, so the extension fires only once."""
    raw = load(cfg).get("silence_wait_until")
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def clear_wait_until(cfg: dict) -> None:
    """Reset the silence window to default (no permanent extension)."""
    with _flock(cfg):
        d = load(cfg)
        if d.pop("silence_wait_until", None) is not None:
            _save(cfg, d)


def get_last_note_ts(cfg: dict) -> str | None:
    """ISO timestamp baseline for the diff-mode Replay section: the newest
    replayed event's ts as of the last rendered note (wake's initial note or
    any free-round tuck-in). None = no note rendered yet this wake -> full
    (epoch-zero) replay."""
    v = load(cfg).get("last_note_ts")
    return str(v) if v else None


def set_last_note_ts(cfg: dict, ts_iso: str) -> None:
    update(cfg, last_note_ts=ts_iso)


def wait_spent(cfg: dict) -> bool:
    """True when the current round has already consumed its one wait (a manual
    wait() or the auto observe gate stamped it). F5: a consecutive empty wait is
    refused while this is True; any activity (tool call / user msg / kick) calls
    restore_wait_quota to clear it. Absent -> False."""
    return bool(load(cfg).get("wait_spent"))


def restore_wait_quota(cfg: dict) -> None:
    """F5 quota restore: clear wait_spent so the next wait() is allowed. Called
    on any non-wait activity round (marrow pretool hook) or external trigger
    (user reset / kick). Best-effort via the advisory lock; only writes on a
    real change."""
    with _flock(cfg):
        d = load(cfg)
        if d.pop("wait_spent", None):
            _save(cfg, d)


def set_next_wake_at(cfg: dict, iso_local: str | None) -> None:
    """Persist the scheduled next-wake instant (local ISO) as the durable ledger.
    The scheduled time must never live only in the sentinel process args: a
    compact/kill loses those, but this survives so the tick reconcile can fire an
    overdue wake. None clears it (e.g. paused, or no schedule)."""
    if iso_local is None:
        with _flock(cfg):
            d = load(cfg)
            if d.pop("next_wake_at", None) is not None:
                _save(cfg, d)
        return
    update(cfg, next_wake_at=iso_local)


def get_next_wake_at(cfg: dict) -> str | None:
    """The recorded next-wake instant (local ISO) or None."""
    v = load(cfg).get("next_wake_at")
    return str(v) if v else None


def clear_next_wake_at(cfg: dict) -> None:
    set_next_wake_at(cfg, None)


def set_paused(cfg: dict, paused: bool) -> None:
    """DND flag: tick reconcile, watchdog, sentinel-fire and injections all
    respect it (no reaps, no wakes, no injections while paused). On unpause,
    overdue ledger alarms fire via the next reconcile."""
    if paused:
        update(cfg, paused=True)
    else:
        with _flock(cfg):
            d = load(cfg)
            if d.pop("paused", None) is not None:
                _save(cfg, d)


def is_paused(cfg: dict) -> bool:
    return bool(load(cfg).get("paused"))


def set_rotated(cfg: dict) -> None:
    """Rotate flag: lie_down sets it when the window grew past the rotate line so
    the NEXT pacemaker wake respawns a fresh window (SIGTERM claude + fresh spawn)
    instead of resuming the oversized one."""
    update(cfg, rotated=True)


def take_rotated(cfg: dict) -> bool:
    """Consume the rotate flag (read-and-clear). True = last lie_down asked the
    next wake to respawn the window fresh."""
    with _flock(cfg):
        d = load(cfg)
        val = bool(d.pop("rotated", False))
        if val:
            _save(cfg, d)
        return val


def is_night_mode(cfg: dict) -> bool:
    """True when the persistent night flag is set (mode == 'night'). The flag
    outlives individual wakes — it is set by lie_down(mode='night') and cleared
    only by the morning kick, so it survives the awake-key clears that fire every
    wake/sleep cycle."""
    return str(load(cfg).get("mode") or "") == "night"


def clear_night_mode(cfg: dict) -> bool:
    """Drop the night flag (read-and-clear) under the advisory lock. Returns True
    if it was set. The morning kick calls this to return to day cadence; a
    no-flag call is a harmless no-op."""
    with _flock(cfg):
        d = load(cfg)
        if d.pop("mode", None) is not None:
            _save(cfg, d)
            return True
        return False


def set_retired_sid(cfg: dict, transcript_path: str | None) -> None:
    """Durably record the claude session UUID (the transcript jsonl stem, same
    convention as window.claude_session_id) that was just retired by a
    rotate — a per-session fact, unlike the one-shot `rotated` flag. Every
    resume path must check its resume target against this before resuming: a
    rotated session handed off and must NEVER be resumed again, even after
    `rotated` itself has already been consumed by an unrelated wake and the
    (also one-shot) `transcript` hint still happens to point at it."""
    sid = Path(str(transcript_path)).stem if transcript_path else None
    update(cfg, retired_sid=sid)


def get_retired_sid(cfg: dict) -> str | None:
    return load(cfg).get("retired_sid")


def get_sentinel_pid(cfg: dict) -> int | None:
    """Recorded pid of the one-shot exact-time wake sentinel (cortex.sentinel),
    or None. Every new lie_down kills this predecessor before arming a fresh one."""
    try:
        v = load(cfg).get("sentinel_pid")
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def set_sentinel_pid(cfg: dict, pid: int) -> None:
    update(cfg, sentinel_pid=pid)


def clear_sentinel_pid(cfg: dict, only_if_pid: int | None = None) -> None:
    """Drop the recorded sentinel pid. only_if_pid = self-guard: the sentinel
    clears its own record only when it still matches (a newer lie_down may have
    already re-armed a different pid). None = unconditional clear."""
    with _flock(cfg):
        d = load(cfg)
        cur = d.get("sentinel_pid")
        if only_if_pid is not None and cur is not None and int(cur) != int(only_if_pid):
            return
        if d.pop("sentinel_pid", None) is not None:
            _save(cfg, d)
