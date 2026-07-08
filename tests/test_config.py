from __future__ import annotations

from pathlib import Path

from cortex import config


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = config.load(tmp_path / "does_not_exist.toml")
    assert cfg["core"]["timezone"] == "Australia/Melbourne"
    assert cfg["paths"]["marrow_db"] == ""
    assert cfg["geofence"]["enabled"] is False
    assert cfg["health"]["enabled"] is False
    assert cfg["knowledgec"]["categories"]["default"] == "uncategorized"


def test_daily_budget_defaults_are_1m_net(tmp_path):
    """Gate + note-line daily budget default to 1M (NET-spend semantics); the
    two must agree with each other."""
    cfg = config.load(tmp_path / "does_not_exist.toml")
    assert cfg["gates"]["daily_budget"]["tokens"] == 1_000_000
    assert cfg["note"]["daily_budget"] == 1_000_000


def test_load_merges_overrides(tmp_path):
    toml_path = tmp_path / "cortex.toml"
    toml_path.write_text(
        """
[core]
timezone = "UTC"

[paths]
geofence_file = "/tmp/geo.txt"

[geofence]
enabled = true

[knowledgec.categories]
"com.example.app" = "dev"
"""
    )
    cfg = config.load(toml_path)
    assert cfg["core"]["timezone"] == "UTC"
    assert cfg["paths"]["geofence_file"] == "/tmp/geo.txt"
    assert cfg["geofence"]["enabled"] is True
    assert cfg["knowledgec"]["categories"]["com.example.app"] == "dev"
    assert cfg["knowledgec"]["categories"]["default"] == "uncategorized"


def test_path_helpers_default_when_empty():
    cfg = config.load(Path("/does/not/exist.toml"))
    assert config.marrow_db_path(cfg) == config.DEFAULT_MARROW_DB
    assert config.knowledgec_db_path(cfg) == config.DEFAULT_KNOWLEDGEC_DB
    assert config.geofence_file_path(cfg) is None
    assert config.health_export_path(cfg) is None
