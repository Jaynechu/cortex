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
DEFAULT_DAY_LOG = Path.home() / ".config" / "marrow" / "day_log.md"
DEFAULT_DAY_LOG_ARCHIVE_DIR = Path.home() / ".config" / "marrow" / "day_log_archive"
DEFAULT_AFFECT_FLAG = Path.home() / ".config" / "marrow" / "cortex" / "affect_flag.json"
DEFAULT_SELF_SCHEDULE = Path.home() / ".config" / "marrow" / "cortex" / "self_schedule.json"
DEFAULT_HANDOFF = Path.home() / ".config" / "marrow" / "cortex" / "handoff.md"
DEFAULT_CORTEX_HOME = Path.home() / ".config" / "marrow" / "cortex"
DEFAULT_NY_DB_PAGES = Path.home() / "Desktop" / "NY" / "db-pages"
DEFAULT_MARROW_REPO = Path.home() / "CC-Lab" / "marrow"
DEFAULT_WAKE_TIMING_LOG = Path.home() / ".config" / "marrow" / "logs" / "wake_timing.log"

_DEFAULTS: dict[str, Any] = {
    "core": {"timezone": "Australia/Melbourne"},
    "paths": {
        "marrow_db": "",
        "knowledgec_db": "",
        "geofence_file": "",
        "health_export": "",
        "day_log": "",
        "day_log_archive_dir": "",
        "affect_flag_file": "",
        "self_schedule_file": "",
        "handoff_file": "",
        "cortex_home": "",
        "wishlist_file": "",
        "ny_db_pages": "",
        "wake_timing_log": "",
    },
    # Per-wake safety valve: cap tokens spent in one wake; breach or the marrow
    # wall-clock timeout (marrow.call_timeout_s) rebirths a fresh session.
    # signal_log = the ear's tail-followed wake signal file (WAKE/NUDGE lines);
    # arm_prompt_path = the launch-time prompt that arms the Monitor ear;
    # ear_timeout_sec = how long the pacemaker waits for a wake to land before
    # respawning; say_sound = the sound say() plays when it fronts the window.
    "wake": {
        "token_cap": 150_000,
        "signal_log": "",
        "arm_prompt_path": "",
        "ear_timeout_sec": 90,
        "say_sound": "Glass",
        # Max wait() calls allowed per wake (reset on wake start / lie_down).
        # A call past this returns a refusal result (not an exception).
        "wait_max_per_wake": 2,
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
        # self_scheduled/affect_flag all silent; only schedule (duty) pierces.
        "night": {"start": "23:00", "end": "06:00", "cap": 0},
        # Daily wake-token budget: once today's SUM(NET spend — cache-miss
        # rewrite + output, ct_wake_log.net_tokens with a tokens fallback)
        # reaches this, self-wakes stop; schedule pierces; resets at local
        # midnight. Net semantics — cache reads are near-free.
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

    # Schedule (duty) blocks: an array of tables ([[schedule]]), not a merged
    # section — pass the user's list straight through (empty when unset).
    sched = loaded.get("schedule", [])
    cfg["schedule"] = sched if isinstance(sched, list) else []

    return cfg


def marrow_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("marrow_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_MARROW_DB


def knowledgec_db_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("knowledgec_db") or ""
    return Path(raw).expanduser() if raw else DEFAULT_KNOWLEDGEC_DB


def geofence_file_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("geofence_file") or ""
    return Path(raw).expanduser() if raw else None


def health_export_path(cfg: dict) -> Path | None:
    raw = cfg["paths"].get("health_export") or ""
    return Path(raw).expanduser() if raw else None


def day_log_path(cfg: dict) -> Path:
    raw = cfg["paths"].get("day_log") or ""
    return Path(raw).expanduser() if raw else DEFAULT_DAY_LOG


def day_log_archive_dir(cfg: dict) -> Path:
    raw = cfg["paths"].get("day_log_archive_dir") or ""
    return Path(raw).expanduser() if raw else DEFAULT_DAY_LOG_ARCHIVE_DIR


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


def wake_timing_log_path(cfg: dict) -> Path:
    """Shared wake-latency probe log (cortex marks + marrow stream-event marks).
    Default: ~/.config/marrow/logs/wake_timing.log."""
    raw = cfg["paths"].get("wake_timing_log") or ""
    return Path(raw).expanduser() if raw else DEFAULT_WAKE_TIMING_LOG


def wake_signal_log_path(cfg: dict) -> Path:
    """The ear's wake-signal file: the persistent Monitor `tail -f`s it, the
    pacemaker appends WAKE/NUDGE lines to it. Default: <cortex_home>/wake_signal.log."""
    raw = cfg["wake"].get("signal_log") or ""
    return Path(raw).expanduser() if raw else cortex_home(cfg) / "wake_signal.log"


def arm_prompt_path(cfg: dict) -> Path:
    """Launch-time prompt that arms the Monitor ear + reads handoff + lies down.
    Default: <cortex_home>/prompts/arm.md."""
    raw = cfg["wake"].get("arm_prompt_path") or ""
    return Path(raw).expanduser() if raw else cortex_home(cfg) / "prompts" / "arm.md"
