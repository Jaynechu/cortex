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
        "cortex_home": "",
        "wishlist_file": "",
        "ny_db_pages": "",
        "wake_timing_log": "",
    },
    # Per-wake safety valve: cap tokens spent in one wake; breach or the marrow
    # wall-clock timeout (marrow.call_timeout_s) rebirths a fresh session.
    "wake": {"token_cap": 150_000},
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
    "tick": {"collect_interval_sec": 1800, "pacemaker_interval_sec": 300},
    # Pacemaker integration knobs (numbers the integration layer computes with).
    "pacemaker": {
        "dry_run": True,
        "active_window_min": 5,
        "at_home_default": True,
        "cal_busy_default": False,
        "token_meter": {"daily_budget_tokens": 0, "window_hours": 24},
    },
    # Pure-pacemaker config (consumed by pacemaker.* modules as top-level keys).
    "desire": {
        "attachment": {
            "base_rate_per_min": 0.002,
            "decay_rate_per_min": 0.0005,
            "busy_multiplier": 0.0,
            "home_free_multiplier": 2.0,
            "gap_threshold_min": 180,
        },
        "curiosity": {"base_rate_per_min": 0.001, "decay_rate_per_min": 0.0005},
        "worry": {"base_rate_per_min": 0.0, "decay_rate_per_min": 0.001},
        "duty": {"base_rate_per_min": 0.001, "decay_rate_per_min": 0.0005},
    },
    "gates": {
        "cooldown_min": 45,
        "daily_message_cap": 12,
        "fatigue_windows": [{"start": "23:30", "end": "07:00"}],
        "token_budget_min_reserve": 0.1,
    },
    "triggers": {
        "desire_thresholds": {"attachment": 0.8, "curiosity": 0.8, "worry": 0.7, "duty": 0.8},
        "floor_interval_min": 60,
        "floor_jitter_min": 10,
    },
    "expect_reply": {
        "check_interval_min": 30,
        "worry_increment": 0.05,
        "tone_levels": ["neutral", "concerned", "worried", "anxious"],
    },
    # Wake bulletin knobs (07-04: battery gauge + forced replay).
    "bulletin": {
        # Hard cap on the whole rendered bulletin text.
        "max_chars": 2000,
        # How many trailing user/assistant conversation pairs to force-append
        # (Decided 07-04: never rely on cortex self-serve recall queries).
        "replay_pairs": 3,
        # Per-message truncation inside a replayed pair, keeps total bounded.
        "replay_pair_chars": 240,
    },
}

_SECTIONS = (
    "core", "paths", "knowledgec", "geofence", "health",
    "tick", "pacemaker", "desire", "gates", "triggers", "expect_reply", "marrow",
    "wake", "bulletin",
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

    categories = dict(_DEFAULTS["knowledgec.categories"])
    loaded_categories = loaded.get("knowledgec", {}).get("categories", {})
    categories.update(loaded_categories)
    cfg["knowledgec"]["categories"] = categories

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
