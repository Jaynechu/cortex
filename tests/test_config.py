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


def test_every_injected_prompt_carries_a_machine_marker(tmp_path):
    """Phase 3 D8: every watcher/system text injected into the cortex window (so
    it lands as a user-role turn) must begin with a recognised machine marker,
    else recall/tl read it as user speech. Grep-level guard over all prompt
    defaults + the fuse constant."""
    from cortex import transcript
    from cortex.watchdog import _DEFAULT_FUSE_PROMPT

    cfg = config.load(tmp_path / "none.toml")
    markers = transcript._line_markers(cfg)  # [CORTEX-WAKE] + machine_line_markers

    def marked(text: str) -> bool:
        return any(m in text for m in markers)

    wake = cfg["wake"]
    assert marked(wake["tuck_in_text"])
    assert marked(wake["ctl_sleep_prompt"])
    assert marked(cfg["gates"]["night"]["close_prompt"])
    assert marked(_DEFAULT_FUSE_PROMPT)
    # the family covers the new fuse / ctl / command markers
    for needle in ("[FUSE]", "[CTL]", "[CMD"):
        assert needle in markers


def test_path_helpers_default_when_empty():
    cfg = config.load(Path("/does/not/exist.toml"))
    assert config.marrow_db_path(cfg) == config.DEFAULT_MARROW_DB
    assert config.knowledgec_db_path(cfg) == config.DEFAULT_KNOWLEDGEC_DB
    assert config.geofence_file_path(cfg) is None
    assert config.health_export_path(cfg) is None
