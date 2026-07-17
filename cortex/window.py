"""iTerm2 window control for the resident interactive cortex session. All
control via iTerm2 AppleScript (works while the screen is locked — no keyboard
simulation). Primitives: ensure_window, respawn (fresh window with the emoji +
bell-marker wake prompt baked in as its first prompt, see fresh_initial_prompt),
append_wake_signal (the ear bell for an already-running resident window),
type_wake_signal (typed rearm on ear death), send_esc, say,
hard_interrupt (process-level SIGINT fallback when esc
alone may not land, e.g. no focus). A fresh window wakes silently — the baked-in
prompt is the only trace, no notification, but carries the same bell marker as
the ear so the marrow UserPromptSubmit hook detects it and injects the full
wakeup note. An alive resident window is woken by the signal-file ear (a Monitor
tailing wake_signal.log) instead. The window body is one `claude` running in
cortex_home with MARROW_CORTEX=1 set explicitly (identity marker).
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

from cortex import config, wake_state

_APP = "iTerm2"
_ITERM_BID = "com.googlecode.iterm2"
# Delay between typing a prompt and pressing Enter. Claude's TUI treats a
# text+newline `write text` as one bracketed paste and swallows the submit, so
# the prompt is typed first (no newline) then Enter is sent as a separate key.
_SUBMIT_DELAY_S = 0.6


class WindowError(Exception):
    pass


def _osa(script: str) -> str:
    p = subprocess.run(["osascript", "-"], input=script,
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise WindowError(p.stderr.strip() or "osascript failed")
    return p.stdout.strip()


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def is_running() -> bool:
    # Plain `application ... is running` never launches the app (unlike `tell`).
    return _osa('return (application "iTerm2" is running)') == "true"


def _frontmost_bid() -> str | None:
    """Bundle id of the current frontmost app, so window creation can restore
    focus (spawn must never steal keyboard focus)."""
    try:
        bid = _osa('tell application "System Events" to get bundle identifier '
                   'of first process whose frontmost is true')
        return bid or None
    except WindowError:
        return None


def _activate_bid(bid: str | None) -> None:
    if bid:
        try:
            _osa(f'tell application id "{bid}" to activate')
        except WindowError:
            pass


def _guard_focus(prev: str | None) -> None:
    """`write text` intermittently raises the iTerm window. If it grabbed focus
    from another app, hand focus back. Only say() is allowed to front cortex."""
    if not prev or prev == _ITERM_BID:
        return
    if _frontmost_bid() == _ITERM_BID:
        _activate_bid(prev)


def _session_alive(sid: str) -> bool:
    script = f'''
tell application "{_APP}"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then return "yes"
      end repeat
    end repeat
  end repeat
end tell
return "no"
'''
    try:
        return _osa(script) == "yes"
    except WindowError:
        return False


def window_model(cfg: dict) -> str:
    """Explicit model for cortex windows — never inherit the (expensive top-tier)
    system default. Reused by every cortex window spawn."""
    return cfg["wake"].get("window_model", "opus")


def window_effort(cfg: dict) -> str:
    """Reasoning effort (low|medium|high|xhigh|max). Empty -> omit the flag."""
    return cfg["wake"].get("window_effort", "")


def wake_prompt(cfg: dict) -> str:
    """The single-line first prompt handed to a fresh cortex window: JUST the
    configured emoji (wake.wake_prompt, default '☀️') so no readable text shows
    in the user's face. The full wake instructions (read the note, arm the ear,
    choose next wake) are injected by marrow's UserPromptSubmit hook when this
    emoji is submitted in a cortex window."""
    return cfg["wake"].get("wake_prompt", "☀️")


def _gen_token_suffix(token) -> str:
    """Wire form of the cancellation-epoch token appended to a wake signal line:
    ' {g<gen>:<state_id>}'. token=None -> "" (legacy token-less line, still
    processed by the consumer). Kept minimal + trailing so the marker substring
    match is unaffected."""
    if not token:
        return ""
    gen, state_id = token
    if gen is None:
        return ""
    return f" {{g{gen}:{state_id}}}"


def wake_signal_line(cfg: dict, now, rearm: bool = False, token=None) -> str:
    """The bell line: '<marker> HH:MM' (local time), optionally carrying the
    cancellation-epoch token as a trailing ' {g<gen>:<sid>}' tag. The marrow
    UserPromptSubmit hook detects the marker and injects the full wakeup note —
    this line is a BELL ONLY, no note body, no read errand. It validates the
    token against the live epoch at consumption (stale -> suppress). `rearm`
    appends the ear-died suffix for the typed re-arm of an alive window whose ear
    missed. The token tag goes AFTER the rearm suffix (trailing)."""
    marker = cfg["wake"].get("wake_signal_marker", "[CORTEX-WAKE]")
    line = f"{marker} {now.strftime('%H:%M')}"
    if rearm:
        line += cfg["wake"].get("rearm_suffix", " (ear died — rearm)")
    line += _gen_token_suffix(token)
    return line


def fresh_initial_prompt(cfg: dict, now) -> str:
    """The baked first prompt for a brand-new/resumed cortex window: the
    configured emoji + the bell marker line, e.g. '☀️ [CORTEX-WAKE] 00:55'.
    Same marker as the ear bell, so the marrow UserPromptSubmit hook detects it
    and injects the full wakeup note — the window gets its wake identity + note
    in one stroke instead of the emoji alone being read as a bare chat message."""
    return f"{wake_prompt(cfg)} {wake_signal_line(cfg, now)}"


def launch_command(cfg: dict, initial_prompt: str | None = None,
                   resume_sid: str | None = None) -> str:
    # Identity + channel markers set explicitly (hooks derive channel from
    # MARROW_CHANNEL; MARROW_CORTEX=1 = cortex identity / kickout immunity).
    # --model/--effort pin tier + reasoning so the window never rides the
    # system default. Reused by every cortex window spawn. A non-empty
    # initial_prompt (fresh_initial_prompt: emoji + bell marker) is baked in as
    # claude's first positional prompt so a freshly launched window starts
    # acting immediately — the marrow hook detects the marker and injects the
    # full note; near-zero readable text (one emoji + a short marker line).
    # A non-empty resume_sid adds `--resume <sid>` so a window that simply died
    # (crash / manual close, NOT a deliberate rotate) comes back as the SAME
    # session with full context — no fresh brain, no handoff catchup needed.
    home = str(config.cortex_home(cfg))
    cmd = cfg["wake"].get("launch_command", "claude")
    flags = f" --model {window_model(cfg)}"
    eff = window_effort(cfg)
    if eff:
        flags += f" --effort {eff}"
    if resume_sid:
        flags += f" --resume {_shq(resume_sid)}"
    # Skip the workspace-trust dialog so the injected note lands (a fresh dir
    # otherwise blocks on the trust prompt). Mirrors marrow's headless call.
    if cfg["wake"].get("skip_permissions", True):
        flags += " --dangerously-skip-permissions"
    arg = f" {_shq(initial_prompt)}" if initial_prompt else ""
    return f"cd {home} && MARROW_CORTEX=1 MARROW_CHANNEL=ct {cmd}{flags}{arg}"


def _shq(text: str) -> str:
    """Single-quote a shell argument (the initial prompt) for the launch command."""
    return "'" + text.replace("'", "'\\''") + "'"


def _append_signal_line(cfg: dict, line: str) -> None:
    p = config.wake_signal_log_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def append_wake_signal(cfg: dict, now, token=None) -> None:
    """Append one bell line the armed Monitor ear picks up: '<marker> HH:MM'
    (plus the cancellation-epoch token tag when `token` is given). The marker
    (not this file) is what the marrow UserPromptSubmit hook detects to inject
    the full wakeup note — a BELL ONLY, no note body, no read errand. The token
    lets the consumer suppress a wake line that a newer epoch already superseded.
    Best-effort: a write failure never crashes the pacemaker."""
    _append_signal_line(cfg, wake_signal_line(cfg, now, token=token))


_launch_command = launch_command  # back-compat alias


def _spawn(cfg: dict, initial_prompt: str | None = None,
           resume_sid: str | None = None) -> str:
    name = _esc(cfg["wake"].get("session_name", "cortex"))
    launch = _esc(launch_command(cfg, initial_prompt, resume_sid))
    # No `activate` — spawning must not steal keyboard focus. Creating a window
    # still brings iTerm forward, so capture the frontmost app and restore it.
    prev = _frontmost_bid()
    script = f'''
tell application "{_APP}"
  set w to (create window with default profile)
  tell current session of w
    set name to "{name}"
    write text "{launch}"
    return id
  end tell
end tell
'''
    sid = _osa(script)
    _activate_bid(prev)
    return sid


def ensure_window(cfg: dict) -> str:
    """Return the live cortex session id, spawning the window if iTerm is not
    running or the persisted session is gone/dead. A session that still exists
    but whose `claude` process died (SIGINT/crash/manual ctrl-C leaves a bare
    shell) is relaunched in place rather than respawned — cheaper, keeps the
    window/geometry, and the shell is otherwise idle so typing the launch
    command is safe. Either path is a new brain; wake.py's _window_rotated
    detects both cases itself (session-dead / claude-dead) BEFORE this runs,
    so no rotate flag is set here (this fn can also fire mid-wake, where
    setting it would wrongly mark the NEXT wake)."""
    sid = wake_state.get_session_id(cfg)
    if sid and is_running() and _session_alive(sid):
        if find_claude_pid(cfg) is not None:
            return sid
        _relaunch(sid, cfg)
        return sid
    sid = _spawn(cfg)
    wake_state.set_session_id(cfg, sid)
    _wait_ready(sid, cfg)  # let the TUI finish booting before the first inject
    return sid


def _relaunch(sid: str, cfg: dict) -> None:
    """Type the launch command into a session sitting at a bare shell (its
    `claude` process died) and wait for the TUI to come back up."""
    _type(sid, launch_command(cfg))
    time.sleep(_SUBMIT_DELAY_S)
    _enter(sid)
    _wait_ready(sid, cfg)


def _close_session(sid: str) -> None:
    """Close a specific iTerm session (the old resident window's tab)."""
    try:
        _osa(_session_stmt(sid, "tell s to close"))
    except WindowError:
        pass


def claude_session_id(cfg: dict) -> str | None:
    """The claude conversation session UUID for --resume: the stem of a
    session jsonl (~/.claude/projects/<cwd>/<uuid>.jsonl). This is NOT the
    iTerm session id (wake_state.session_id).

    Priority: the newest WINDOW-LINEAGE session jsonl in the transcript dir
    FIRST (transcript.newest_window_lineage) — the newest jsonl whose first
    user message carries the wake signal marker, i.e. was launched as a cortex
    window (fresh_initial_prompt bakes it into every window's first prompt
    since dccb3d4). Plain newest() is NOT enough: the transcript dir also holds
    HEADLESS session archives (marrow's sessionend digest runs `claude -p`
    against this same cwd -> same projects dir), and a digest run can be the
    mtime-newest file — resuming it exposes its full worker prompt in the
    window (live-confirmed). The recorded hint is a best-effort bounded poll
    captured right after a spawn (_wait_new_transcript, ~8s) — the claude TUI
    can take 30s+ to create its session jsonl, so in real timing the poll
    routinely times out (hint None) AND, if a stale entry from a previous cycle
    was never cleared, the hint can be present but wrong (live-confirmed:
    resumed a stale recorded uuid instead of the dead window's real archive).
    The hint is now only a fallback for when no marker-bearing transcript file
    exists at all. None only when neither yields a UUID (caller falls back to
    a fresh spawn)."""
    from pathlib import Path
    from cortex import transcript as _transcript

    marker = cfg.get("wake", {}).get("wake_signal_marker", "[CORTEX-WAKE]")
    lineage = _transcript.newest_window_lineage(cfg, marker)
    if lineage is not None:
        return lineage.stem
    raw = wake_state.load(cfg).get("transcript")
    if raw:
        stem = Path(str(raw)).stem
        if stem:
            return stem
    return None


def respawn(cfg: dict, initial_prompt: str | None = None,
            resume_sid: str | None = None) -> str:
    """Replace the resident window with a new one: SIGTERM its `claude` process
    (never SIGKILL), close the old iTerm session, then spawn. A non-empty
    initial_prompt (fresh_initial_prompt: emoji + bell marker) is baked into the
    launch command so the window starts acting immediately — no arm prompt, no
    lie-down-first, no signal. A non-empty resume_sid launches `claude --resume
    <sid>` (same conversation, full context) instead of a fresh brain — used
    when the window simply died with no rotate flag. Persists and returns the
    new resident sid. Reused for rotate/rebirth (fresh) and the dead-window
    recovery (resume)."""
    pid = find_claude_pid(cfg)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    old = wake_state.get_session_id(cfg)
    if old:
        _close_session(old)
    sid = _spawn(cfg, initial_prompt, resume_sid)
    wake_state.set_session_id(cfg, sid)
    _wait_ready(sid, cfg)
    return sid


def _read_session(sid: str) -> str:
    script = f'''
tell application "{_APP}"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then return (text of s)
      end repeat
    end repeat
  end repeat
end tell
return ""
'''
    try:
        return _osa(script)
    except WindowError:
        return ""


def _wait_ready(sid: str, cfg: dict) -> None:
    """Block until the freshly spawned claude TUI is ready for input (its footer
    marker appears), so the first injection never types into a booting shell."""
    marker = cfg["wake"].get("ready_marker", "accept edits")
    timeout = float(cfg["wake"].get("ready_timeout_sec", 30))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker in _read_session(sid):
            return
        time.sleep(1.0)


def _session_stmt(sid: str, stmt: str) -> str:
    return f'''
tell application "{_APP}"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then
          {stmt}
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no"
'''


def _type(sid: str, text: str) -> None:
    """Type text into the session WITHOUT a trailing newline (no submit)."""
    if _osa(_session_stmt(sid, f'tell s to write text "{_esc(text)}" newline no')) != "ok":
        raise WindowError(f"session {sid} not found for write")


def _enter(sid: str) -> None:
    """Send a bare carriage return (submit the current input)."""
    _osa(_session_stmt(sid, "tell s to write text (character id 13) newline no"))


def _submit_prompt(sid: str, text: str) -> None:
    # Type once (avoid double-typing), then Enter twice: a first-run startup
    # notice can swallow the first Enter, leaving the prompt unsubmitted; the
    # second Enter is a harmless no-op on an already-empty input line.
    _type(sid, text)
    time.sleep(_SUBMIT_DELAY_S)
    _enter(sid)
    time.sleep(0.3)
    _enter(sid)


def write_note(cfg: dict, text: str):
    """Persist the wakeup note to its file and return the path. The ear-based
    wake references this path in the signal line (no typing); the marrow hook
    reads it to inject the full note when it sees the bell marker."""
    note_path = wake_state.wakeup_note_path(cfg)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(text)
    return note_path


def inject_prompt(cfg: dict, text: str) -> bool:
    """Inject a one-line text prompt into the resident cortex window, restoring
    focus afterwards. Used by the fuse path to ask the session to write its
    handoff and lie down. Returns False if there is no resident session."""
    sid = wake_state.get_session_id(cfg)
    if not sid:
        return False
    prev = _frontmost_bid()
    try:
        _submit_prompt(sid, text)
    except WindowError:
        return False
    finally:
        _guard_focus(prev)
    return True


def type_wake_signal(cfg: dict, now) -> bool:
    """Ear-died rearm (ladder 2a): type the bell line '<marker> HH:MM (ear died
    — rearm)' into the ALIVE resident window. It flows through the marrow hook
    like any wake (marker detected -> note injected -> session rearms). Returns
    False if there is no resident session. Focus-guarded like every typing path."""
    return inject_prompt(cfg, wake_signal_line(cfg, now, rearm=True))


def deliver_covert_marker(cfg: dict, marker_line: str) -> str:
    """Deliver a machine-marker line to the resident window the SAME covert way a
    wake bell reaches it: append it to wake_signal.log so the armed Monitor ear
    surfaces ONLY the marker (the full instruction body is injected invisibly by
    the marrow UserPromptSubmit hook keyed on the marker). The visible round is
    just the short marker line — never the prompt body.

    Reuses the wake ladder rung order: BELL first (log append); typed fallback
    (inject_prompt) ONLY when the ear did not pick the bell up within
    ear_timeout_sec — the accepted physical last resort. Returns the rung used:
    'bell' | 'typed' | 'none' (no resident window to type into)."""
    from cortex import transcript

    before = transcript.mtime(cfg)
    _append_signal_line(cfg, marker_line)
    timeout = float(cfg["wake"].get("ear_timeout_sec", 90))
    step = 3.0
    waited = 0.0
    while waited < timeout:
        time.sleep(min(step, timeout - waited))
        waited += step
        after = transcript.mtime(cfg)
        if after is not None and (before is None or after > before):
            return "bell"
    # Ear missed the bell -> typed fallback (last resort). The typed marker line
    # still flows through the marrow hook (marker detected -> body injected).
    return "typed" if inject_prompt(cfg, marker_line) else "none"


def send_esc(cfg: dict) -> None:
    """Interrupt the current turn (ESC, char id 27, no trailing newline)."""
    sid = wake_state.get_session_id(cfg)
    if sid:
        prev = _frontmost_bid()
        _osa(_session_stmt(sid, "tell s to write text (character id 27) newline no"))
        _guard_focus(prev)


def _session_tty(sid: str) -> str | None:
    """tty device (e.g. /dev/ttys003) of the resident session, via iTerm2."""
    try:
        out = _osa(_session_stmt(sid, "return (tty of s)"))
    except WindowError:
        return None
    return out if out.startswith("/dev/") else None


def _ps_tty_claude_pids(ttyname: str) -> list[int]:
    """pid(s) whose exact command is `claude` on the given tty (name without
    the /dev/ prefix, e.g. ttys003)."""
    try:
        p = subprocess.run(["ps", "-t", ttyname, "-o", "pid=,comm="],
                           capture_output=True, text=True)
    except OSError:
        return []
    if p.returncode != 0:
        return []
    pids = []
    for line in p.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == "claude":
            try:
                pids.append(int(parts[0]))
            except ValueError:
                continue
    return pids


def _pgrep_claude_pids() -> list[int]:
    try:
        p = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True)
    except OSError:
        return []
    if p.returncode not in (0, 1):  # 1 = no matches, still a clean run
        return []
    return [int(x) for x in p.stdout.split() if x.isdigit()]


def _pid_cwd(pid: int) -> str | None:
    try:
        p = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                           capture_output=True, text=True)
    except OSError:
        return None
    if p.returncode != 0:
        return None
    for line in p.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def find_claude_pid(cfg: dict) -> int | None:
    """Discover the pid of the resident cortex window's `claude` process.
    (a) iTerm session tty -> ps -t <tty> for a `claude` command on that tty.
    (b) fallback: pgrep -x claude, keep the ones whose cwd == cortex_home.
    Ambiguous (0 or >1 candidates) or undiscoverable -> None (never guess)."""
    sid = wake_state.get_session_id(cfg)
    if sid:
        tty = _session_tty(sid)
        if tty:
            pids = _ps_tty_claude_pids(tty.removeprefix("/dev/"))
            if len(pids) == 1:
                return pids[0]

    home = str(config.cortex_home(cfg))
    candidates = [pid for pid in _pgrep_claude_pids() if _pid_cwd(pid) == home]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _claude_on_session_tty(cfg: dict, sid: str) -> bool:
    """True iff a `claude` process runs on the RECORDED iTerm session's own tty.
    Per-session liveness — the cwd fallback in find_claude_pid is deliberately
    NOT used, so another claude window in cortex_home can't fake this session
    alive. No tty (session gone) -> False."""
    tty = _session_tty(sid)
    if not tty:
        return False
    return bool(_ps_tty_claude_pids(tty.removeprefix("/dev/")))


def hard_interrupt(cfg: dict) -> int | None:
    """Guaranteed esc-equivalent: SIGINT the resident window's claude process.
    Never SIGKILL. Returns the signaled pid, or None if discovery was
    ambiguous/failed (skip rather than signal an unverified pid)."""
    pid = find_claude_pid(cfg)
    if pid is None:
        return None
    try:
        os.kill(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        return None
    return pid


def say(cfg: dict, note: str | None = None) -> None:
    """开口 primitive: the attention signal. Fronts the resident cortex iTerm
    window and plays a sound (the words themselves are the normal in-window
    reply). This is the SOLE place cortex is allowed to take keyboard focus —
    every other path guards focus. `note` is accepted for CLI/API symmetry but
    the words live in the window; only the sound + front happen here."""
    _play_sound(cfg.get("wake", {}).get("say_sound", ""))
    _bring_to_front(wake_state.get_session_id(cfg))


def _play_sound(name: str) -> None:
    """Play a named macOS system sound (afplay on the .aiff under System/Library
    Sounds); empty name -> silent. Best-effort, never raises."""
    if not name:
        return
    path = f"/System/Library/Sounds/{name}.aiff"
    try:
        subprocess.Popen(["afplay", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _bring_to_front(sid: str | None) -> None:
    """Opt-in only: front the cortex window (the sole allowed activate of it)."""
    if not sid:
        return
    script = f'''
tell application "{_APP}"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (id of s) is "{sid}" then
          select w
          tell t to select
          tell s to select
          return "ok"
        end if
      end repeat
    end repeat
  end repeat
end tell
return "no"
'''
    try:
        _osa(script)
    except WindowError:
        pass
