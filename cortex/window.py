"""iTerm2 window control for the resident interactive cortex session. All
control via iTerm2 AppleScript (works while the screen is locked — no keyboard
simulation). Primitives: ensure_window, respawn, append_wake_signal (the ear),
inject_note (schedule windows only), send_esc, say, hard_interrupt (process-level
SIGINT fallback when esc alone may not land, e.g. no focus). The resident window
is woken by a signal-file ear (a Monitor tailing wake_signal.log), not by typing.
The window body is one `claude` running in cortex_home with MARROW_CORTEX=1 set
explicitly (identity marker).
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
    system default. Reused by every cortex window spawn (schedule/review too)."""
    return cfg["wake"].get("window_model", "opus")


def window_effort(cfg: dict) -> str:
    """Reasoning effort (low|medium|high|xhigh|max). Empty -> omit the flag."""
    return cfg["wake"].get("window_effort", "")


def arm_prompt(cfg: dict) -> str:
    """The launch-time initial prompt that arms the Monitor ear, reads the
    handoff, and lies down. Read from arm_prompt_path with {signal_log}
    substituted for the live signal-log path. Missing/unreadable -> empty
    string (window still launches, just unarmed)."""
    path = config.arm_prompt_path(cfg)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return text.replace("{signal_log}", str(config.wake_signal_log_path(cfg)))


def launch_command(cfg: dict) -> str:
    # Identity + channel markers set explicitly (hooks derive channel from
    # MARROW_CHANNEL; MARROW_CORTEX=1 = cortex identity / kickout immunity).
    # --model/--effort pin tier + reasoning so the window never rides the
    # system default. Reused by every cortex window spawn. A non-empty arm
    # prompt is appended as claude's initial positional prompt so a freshly
    # launched window arms its ear + reads handoff without any typing.
    home = str(config.cortex_home(cfg))
    cmd = cfg["wake"].get("launch_command", "claude")
    flags = f" --model {window_model(cfg)}"
    eff = window_effort(cfg)
    if eff:
        flags += f" --effort {eff}"
    # Skip the workspace-trust dialog so the injected note lands (a fresh dir
    # otherwise blocks on the trust prompt). Mirrors marrow's headless call.
    if cfg["wake"].get("skip_permissions", True):
        flags += " --dangerously-skip-permissions"
    arm = arm_prompt(cfg)
    arg = f" {_shq(arm)}" if arm else ""
    return f"cd {home} && MARROW_CORTEX=1 MARROW_CHANNEL=ct {cmd}{flags}{arg}"


def _shq(text: str) -> str:
    """Single-quote a shell argument (the arm prompt) for the launch command."""
    return "'" + text.replace("'", "'\\''") + "'"


def _append_signal_line(cfg: dict, line: str) -> None:
    p = config.wake_signal_log_path(cfg)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def append_wake_signal(cfg: dict, note_path: str) -> None:
    """Append one WAKE line the armed Monitor ear picks up: 'Waking up — read
    <note_path> first'. The wake reason already lives inside the note itself
    (bulletin's Wake: line) so it is not duplicated here. Best-effort: a write
    failure never crashes the pacemaker."""
    _append_signal_line(cfg, f"Waking up — read {note_path} first")


def append_nudge_signal(cfg: dict, wrap_line: str) -> None:
    """Append one NUDGE line (watchdog wrap-up nudge) the ear picks up."""
    _append_signal_line(cfg, f"NUDGE {wrap_line}")


_launch_command = launch_command  # back-compat alias


def _spawn(cfg: dict) -> str:
    name = _esc(cfg["wake"].get("session_name", "cortex"))
    launch = _esc(launch_command(cfg))
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
    so no rotate flag is set here (this fn also fires mid-wake for the
    watchdog wrap nudge, where setting it would wrongly mark the NEXT wake)."""
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


def spawn_fresh(cfg: dict) -> str:
    """Spawn a brand-new cortex window (attention hygiene for schedule duties —
    no roaming context, no 碎碎念). NOT the resident session: its sid is never
    persisted, so it can't be resumed and cortex ends it itself when done."""
    sid = _spawn(cfg)
    _wait_ready(sid, cfg)
    return sid


def _close_session(sid: str) -> None:
    """Close a specific iTerm session (the old resident window's tab)."""
    try:
        _osa(_session_stmt(sid, "tell s to close"))
    except WindowError:
        pass


def respawn(cfg: dict) -> str:
    """Replace the resident window with a fresh brain: SIGTERM its `claude`
    process (never SIGKILL), close the old iTerm session, then spawn a new
    window (which self-arms via the launch-time arm prompt). Persists and
    returns the new resident sid. Reused for rotate, rebirth and the ear
    recovery path — one fresh-brain path, no /clear typing."""
    pid = find_claude_pid(cfg)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    old = wake_state.get_session_id(cfg)
    if old:
        _close_session(old)
    sid = _spawn(cfg)
    wake_state.set_session_id(cfg, sid)
    _wait_ready(sid, cfg)
    return sid


def submit_prompt_to(sid: str, cfg: dict, text: str) -> None:
    """Inject one prompt into a specific (non-resident) session, restoring
    focus afterwards. Used for schedule windows keyed by their own sid."""
    prev = _frontmost_bid()
    _submit_prompt(sid, text)
    _guard_focus(prev)


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
    wake references this path in the signal line (no typing); schedule windows
    still type a Read line via inject_note."""
    note_path = wake_state.wakeup_note_path(cfg)
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text(text)
    return note_path


def inject_note(cfg: dict, text: str, sid: str | None = None) -> None:
    """Deliver the multi-line wakeup note as ONE prompt: write it to a file,
    then inject a single line telling cortex to read that file. `write text`
    submits each newline separately, so file transit is the reliable path.
    `sid` targets a specific (e.g. schedule) window; None = the resident one.
    Used only by schedule (fresh duty) windows now — the resident window is
    woken by the signal-file ear, not by typing."""
    prev = _frontmost_bid()
    if sid is None:
        sid = ensure_window(cfg)
    note_path = write_note(cfg, text)
    line = f"Read {note_path} — this is your wakeup note; act on it"
    _submit_prompt(sid, line)
    _guard_focus(prev)


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
