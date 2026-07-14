"""Config loader: ~/.config/marrow/cortex.toml (override via CORTEX_CONFIG env).

Tolerant: missing file or missing keys fall back to defaults. Never raises
on a missing config file.
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "marrow" / "cortex.toml"
DEFAULT_MARROW_DB = Path.home() / ".config" / "marrow" / "marrow.db"
DEFAULT_KNOWLEDGEC_DB = (
    Path.home() / "Library" / "Application Support" / "Knowledge" / "knowledgeC.db"
)
DEFAULT_AFFECT_FLAG = Path.home() / ".config" / "marrow" / "cortex" / "affect_flag.json"
DEFAULT_SELF_SCHEDULE = Path.home() / ".config" / "marrow" / "cortex" / "self_schedule.json"
DEFAULT_HANDOFF = Path.home() / ".config" / "marrow" / "cortex" / "handoff.md"
DEFAULT_CORTEX_HOME = Path.home() / ".config" / "marrow" / "cortex"
DEFAULT_NY_DB_PAGES = Path.home() / "Desktop" / "NY" / "db-pages"
DEFAULT_MARROW_REPO = Path.home() / "CC-Lab" / "marrow"
DEFAULT_WAKE_TIMING_LOG = Path.home() / ".config" / "marrow" / "logs" / "wake_timing.log"

# Single source of truth for the machine-line marker family (wake bell / free-
# round / night / fuse / ctl / slash-command). Referenced by _DEFAULTS below AND
# by transcript._line_markers' fallback so the two can never drift.
DEFAULT_MACHINE_LINE_MARKERS = ["[TUCK-IN]", "[NEW ROUND]", "[NIGHT]",
                                "[FUSE]", "[CTL]", "[CMD"]

_DEFAULTS: dict[str, Any] = {
    "core": {"timezone": "Australia/Melbourne"},
    "paths": {
        "marrow_db": "",
        "knowledgec_db": "",
        "geofence_file": "",
        "health_export": "",
        "affect_flag_file": "",
        "self_schedule_file": "",
        "handoff_file": "",
        "cortex_home": "",
        "wishlist_file": "",
        "ny_db_pages": "",
        "wake_timing_log": "",
        "wake_audit_log": "",
    },
    # Per-wake safety valve: cap tokens spent in one wake; breach or the marrow
    # wall-clock timeout (marrow.call_timeout_s) forces a fresh session next wake.
    # signal_log = the ear's tail-followed wake signal file (alive-resident wake);
    # ear_timeout_sec = how long the pacemaker waits for an alive-window wake to
    # land before respawning fresh; wake_prompt = the first prompt baked into a
    # freshly spawned window — JUST an emoji so nothing readable shows in the
    # user's face; the full wake instructions are injected by marrow's
    # UserPromptSubmit hook when this exact emoji is submitted in a cortex
    # window (the note path itself is read from config, not this prompt);
    # say_sound = the sound say() plays when it fronts the window.
    "wake": {
        "token_cap": 150_000,
        "signal_log": "",
        "ear_timeout_sec": 90,
        "wake_prompt": "☀️",
        # Bell marker line the marrow UserPromptSubmit hook detects to inject the
        # full wakeup note. Signal is a BELL ONLY — no note body, no read errand.
        # Rendered as "<marker> HH:MM" (local time). rearm_suffix is appended when
        # the ear died and the pacemaker re-types the marker into an alive window.
        "wake_signal_marker": "[CORTEX-WAKE]",
        "rearm_suffix": " (ear died — rearm)",
        "say_sound": "Glass",
        # Max wait() calls allowed per wake (reset on wake start / lie_down).
        # 0 = uncapped (permanent residency; night gate / 150k fuse / grace
        # auto-lie / user return are the backstops).
        "wait_max_per_wake": 0,
        # wait() clamp bounds (minutes). Own bounds, decoupled from the floor
        # draw window (triggers.floor_*): wait guards the hot cache TTL. Floor 16 >
        # silence gate (15) keeps wait strictly "post-gate renewal".
        "wait_min": 16,
        "wait_max": 55,
        # lie_down(next_wake_min=N) clamp (minutes): [next_wake_min, next_wake_max]
        # normally; a rotate short-sleep lowers the floor to next_wake_rotate_min.
        "next_wake_min": 90,
        "next_wake_rotate_min": 16,
        "next_wake_max": 360,
        # Exact-time wake: arm cortex.sentinel (one-shot detached sleep-then-tick)
        # at every lie_down. false = tick-only (launchd 5-min fallback).
        "sentinel": True,
        # Free-round marker appended to wake_signal.log at the silence gate or a
        # wait(N)-expiry. {mins} = real minutes since the user's last message;
        # {user} = marrow user_name (fallback "the user").
        "tuck_in_text":
            "⏳ [NEW ROUND] {mins} min since {user}'s last message. Choose again: "
            "1) play around (playbook); 2) wait(N) (16-55min); "
            "3) lie_down(next_wake_min=N) (90-360min). Feel free to do anything. "
            "No need to wait for reply - {user} will wake you the moment she's back.",
        # Markers that identify a NON-user turn (wake bell / free-round / night /
        # fuse / ctl / slash-command line), so they never reset the silence timer
        # and downstream memory drops them. wake_signal_marker is added
        # automatically. Substring match, so "[CMD" catches every ⚙️ [CMD ct-*].
        "machine_line_markers": list(DEFAULT_MACHINE_LINE_MARKERS),
        # When a declared wait(N) expires, append a freshly rendered wakeup note
        # to the free-round marker (note content only, no behavioural nudge).
        "wait_expiry_note": True,
        # Manual `cortex.ctl sleep` instruction injected into a live window.
        # {mins} = next_wake_min; {rotate} = "write your handoff then " when
        # --rotate, else ""; {rotate_arg} = ", rotate=true" when --rotate, else "".
        "ctl_sleep_prompt":
            "⚙️ [CTL] Wrap up this turn: "
            "{rotate}lie_down(next_wake_min={mins}{rotate_arg}).",
    },
    # marrow repo invocation for the wake call (separate venv/deps, C3).
    "marrow": {
        "repo_dir": str(DEFAULT_MARROW_REPO),
        "venv_python": str(DEFAULT_MARROW_REPO / ".venv" / "bin" / "python"),
        # Inner claude-call budget (s), passed down to marrow; the outer
        # subprocess kill = this + margin. Must match marrow's own default.
        "call_timeout_s": 600,
    },
    "knowledgec": {"stream_name": "/app/usage"},
    "knowledgec.categories": {"default": "uncategorized"},
    "geofence": {"enabled": False},
    "health": {"enabled": False},
    # launchd tick cadence (seconds). Baked into plists at install time.
    "tick": {
        "collect_interval_sec": 1800,
        "pacemaker_interval_sec": 300,
        # OAuth usage % snapshot (marrow subprocess) each collect tick.
        "usage_snapshot": True,
    },
    # Pacemaker integration knobs (numbers the integration layer computes with).
    "pacemaker": {
        "dry_run": True,
        "active_window_min": 5,
        "at_home_default": True,
        "cal_busy_default": False,
    },
    "gates": {
        # Night window (plan 07-08): zero self-wakes 23-06 — floor/
        # self_scheduled/affect_flag all silent.
        # close_prompt = wrap-up instruction injected once into a still-awake
        # resident window when the night window opens (write handoff + lie_down).
        "night": {
            "start": "23:00", "end": "06:00", "cap": 0,
            "close_prompt": "⏳ [NIGHT] Night window is open — one full sleep now. "
                            "Write your handoff entry, then lie_down to end this wake.",
        },
        # Daily wake-token budget: once Cortex Today (sum of today's finished-
        # window final context occupancies + the current live window occupancy)
        # reaches this, self-wakes stop; resets at local midnight.
        "daily_budget": {"tokens": 1_000_000},
    },
    "triggers": {
        # Wake-window draw (minutes) from lie-down. lie_down picks the next wake:
        # an explicit choice clamped to [min, max] (max = cache-TTL guard, min =
        # anti-thrash), or a uniform "dice" draw within the window when omitted.
        # Also the clamp for a model-declared watchdog silence window.
        "floor_min_min": 10,
        "floor_max_min": 55,
    },
    # Wakeup note knobs. Every field is deterministic now, so the old whole-note
    # max_chars cap is gone; per-source limits below keep each line bounded.
    # OSS: identity/display strings stay in config, never hardcoded in .py.
    "note": {
        # Optional first line of the wakeup note (e.g. a nickname for the
        # note), followed by a blank line then the usual content. "" omits it.
        "title": "",
        # Trailing conversation events force-appended to the Replay block
        # (cross-session, uniform, no decay). 4 = two round-trips.
        "replay_events": 4,
        # Per-event truncation inside the Replay block.
        "replay_event_chars": 300,
        # Daily wake-token (NET spend) budget the "Cortex Today X/Y" segment
        # renders against — must match gates.daily_budget.tokens (display=gate).
        "daily_budget": 1_000_000,
        # Pending self-schedule entries surface only when due within this window.
        "pending_window_min": 15,
        # Prior window force-slept without a handoff -> backfill hint line.
        "force_slept_catchup_text":
            "Prior window was force-slept — catchup by recall all events from DB "
            "(do not read raw jsonl) and append to handoff.md",
        # Prior window DIED (crash/manual close) mid-wake without writing its
        # handoff -> the fresh respawn recovers context from its transcript.
        "died_no_handoff_catchup_text":
            "Previous window died without a handoff — recover context from its "
            "transcript, then write the handoff.",
        # One-line turn-end reminder appended at the very end of every rendered
        # note. "" omits it.
        "turn_end_text":
            "NOTE: Call MCP tool to wait or lie_down at the end of each turn. "
            "Wait=wait(N) [N=16-55]; sleep=lie_down(next_wake_min=N) "
            "[90-360; rotate=True unlocks ≥16]. "
            "Skip call = sleep in 5 mins. Auto timer is on during active chat "
            "- no call needed.",
        # Header written into a freshly-created wishlist.md (append-only file,
        # never overwritten). Display text — customise freely.
        "wishlist_header":
            "# Wishlist\n\n(owed treats / wants / self-rewards — append-only)\n",
    },
}

_SECTIONS = (
    "core", "paths", "knowledgec", "geofence", "health",
    "tick", "pacemaker", "gates", "triggers", "marrow",
    "wake", "note",
)


def _config_path() -> Path:
    override = os.environ.get("CORTEX_CONFIG")
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH


def _merge(defaults: dict, loaded: dict) -> dict:
    merged = dict(defaults)
    for key, val in loaded.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load(path: Path | None = None) -> dict[str, Any]:
    """Load cortex config with tolerant fallback to defaults."""
    cfg_path = path or _config_path()
    loaded: dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as f:
            loaded = tomllib.load(f)

    cfg = {section: copy.deepcopy(_DEFAULTS[section]) for section in _SECTIONS}
    for section in _SECTIONS:
        if section in loaded:
            cfg[section] = _merge(cfg[section], loaded[section])

    # Legacy fallback: an old config.toml may still carry the pre-rename
    # [bulletin] section (renamed to [note]) — merge it in if present.
    if "bulletin" in loaded:
        cfg["note"] = _merge(cfg["note"], loaded["bulletin"])

    categories = dict(_DEFAULTS["knowledgec.categories"])
    loaded_categories = loaded.get("knowledgec", {}).get("categories", {})
    categories.update(loaded_categories)
    cfg["knowledgec"]["categories"] = categories

    return cfg


def marrow_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("marrow_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_MARROW_DB


def user_name(cfg: dict, default: str = "the user") -> str:
    """The user's name, read from marrow's config.toml (sibling of cortex.toml in
    the shared config dir) — cortex inherits it, never carries its own copy (OSS:
    no hardcoded persona in code). `default` when marrow config is absent/blank."""
    db = marrow_db_path(cfg)
    marrow_cfg = db.parent / "config.toml"
    try:
        if marrow_cfg.exists():
            with marrow_cfg.open("rb") as f:
                data = tomllib.load(f)
            name = str(data.get("user_name") or "").strip()
            if name:
                return name
    except (OSError, ValueError):
        pass
    return default


def knowledgec_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("knowledgec_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_KNOWLEDGEC_DB


def geofence_file_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("geofence_file") or ""
    return Path(raw).expanduser() if raw else None


def health_export_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("health_export") or ""
    return Path(raw).expanduser() if raw else None


def affect_flag_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("affect_flag_file") or ""
    return Path(raw).expanduser() if raw else DEFAULT_AFFECT_FLAG


def self_schedule_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("self_schedule_file") or ""
    return Path(raw).expanduser() if raw else DEFAULT_SELF_SCHEDULE


def handoff_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("handoff_file") or ""
    return Path(raw).expanduser() if raw else DEFAULT_HANDOFF


def cortex_home(cfg: dict) -> Path:
    """cwd for the resumed full-env marrow cortex session (Decided 07-03 pm)."""
    raw = cfg["paths"].get("cortex_home") or ""
    return Path(raw).expanduser() if raw else DEFAULT_CORTEX_HOME


def wishlist_path(cfg: dict) -> Path:
    """Pure append-only md, fixed path (Decided 07-03 eve). Mirrors marrow's
    own [cortex].wishlist_path default ('' -> <home>/wishlist.md)."""
    raw = cfg["paths"].get("wishlist_file") or ""
    return Path(raw).expanduser() if raw else cortex_home(cfg) / "wishlist.md"


def ny_db_pages_dir(cfg: dict) -> Path:
    raw = cfg["paths"].get("ny_db_pages") or ""
    return Path(raw).expanduser() if raw else DEFAULT_NY_DB_PAGES


def wishlist_header(cfg: dict) -> str:
    """Header written into a freshly-created wishlist.md. Display text; config-driven."""
    return cfg.get("note", {}).get("wishlist_header") or _DEFAULTS["note"]["wishlist_header"]


def wake_timing_log_path(cfg: dict) -> Path:
    """Shared wake-latency probe log (cortex marks + marrow stream-event marks).
    Default: ~/.config/marrow/logs/wake_timing.log."""
    raw = cfg["paths"].get("wake_timing_log") or ""
    return Path(raw).expanduser() if raw else DEFAULT_WAKE_TIMING_LOG


def wake_signal_log_path(cfg: dict) -> Path:
    """The ear's wake-signal file: the persistent Monitor `tail -f`s it, the
    pacemaker appends WAKE/NUDGE lines to it (alive-resident wake only).
    Default: <cortex_home>/wake_signal.log."""
    raw = cfg["wake"].get("signal_log") or ""
    return Path(raw).expanduser() if raw else cortex_home(cfg) / "wake_signal.log"


def wake_audit_log_path(cfg: dict) -> Path:
    """Wake-state audit trail (alarm epoch/generation events). Byte-shared with
    marrow's [cortex].wake_audit_log_file so both sides append to one file.
    Default: <cortex_home>/wake_audit.log. Override via [paths].wake_audit_log."""
    raw = cfg["paths"].get("wake_audit_log") or ""
    return Path(raw).expanduser() if raw else cortex_home(cfg) / "wake_audit.log"
