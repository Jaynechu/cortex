"""Config loader: ~/.config/marrow/cortex.toml (override via CORTEX_CONFIG env).

Tolerant: missing file or missing keys fall back to defaults. Never raises
on a missing config file.
"""
from __future__ import annotations

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

_DEFAULTS: dict[str, Any] = {
    "core": {"timezone": "Australia/Melbourne"},
    "paths": {
        "marrow_db": "",
        "knowledgec_db": "",
        "geofence_file": "",
        "health_export": "",
        "day_log": "",
        "day_log_archive_dir": "",
    },
    "knowledgec": {"stream_name": "/app/usage"},
    "knowledgec.categories": {"default": "uncategorized"},
    "geofence": {"enabled": False},
    "health": {"enabled": False},
}


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

    cfg = {
        "core": dict(_DEFAULTS["core"]),
        "paths": dict(_DEFAULTS["paths"]),
        "knowledgec": dict(_DEFAULTS["knowledgec"]),
        "geofence": dict(_DEFAULTS["geofence"]),
        "health": dict(_DEFAULTS["health"]),
    }
    for section in ("core", "paths", "knowledgec", "geofence", "health"):
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
