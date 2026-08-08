from __future__ import annotations

from pathlib import Path

import pytest

from cortex import config


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = config.load(tmp_path / "does_not_exist.toml")
    assert cfg["core"]["timezone"] == ""  # empty = follow OS timezone
    assert cfg["paths"]["marrow_db"] == ""
    assert cfg["geofence"]["enabled"] is False
    assert cfg["health"]["enabled"] is False
    assert cfg["knowledgec"]["categories"]["default"] == "uncategorized"


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
    """Phase 3 D8: every watcher/system line written into the cortex window (so
    it lands as a user-role turn) must begin with a recognised machine marker,
    else recall/tl read it as user speech. Grep-level guard over all marker/prompt
    lines. FUSE/CTL bodies now live marrow-side and are injected covertly — cortex
    only writes their MARKER lines, which must be marked."""
    from cortex import transcript

    cfg = config.load(tmp_path / "none.toml")
    markers = transcript._line_markers(cfg)  # bell prefix + machine_line_markers

    def marked(text: str) -> bool:
        return any(m in text for m in markers)

    wake = cfg["wake"]
    assert marked(wake["tuck_in_text"])
    assert marked(wake["fuse_marker"])
    assert marked(wake["ctl_sleep_marker"])
    # the family covers the new fuse / ctl / command markers
    for needle in ("[FUSE]", "[CTL]", "[CMD"):
        assert needle in markers


def test_path_helpers_default_when_empty():
    cfg = config.load(Path("/does/not/exist.toml"))
    assert config.marrow_db_path(cfg) == config.DEFAULT_MARROW_DB
    assert config.knowledgec_db_path(cfg) == config.DEFAULT_KNOWLEDGEC_DB
    assert config.geofence_file_path(cfg) is None
    assert config.health_export_path(cfg) is None


def test_handoff_defaults_to_the_single_shared_page():
    """One handoff for every shell (marrow [cortex].handoff_file) — no per-shell
    name, so the fuse's handoff check watches the file the session writes."""
    cfg = config.load(Path("/does/not/exist.toml"))
    assert cfg["paths"]["handoff_file"] == ""  # unset -> DEFAULT_HANDOFF
    assert config.DEFAULT_HANDOFF.name == "handoff.md"


def test_user_name_reads_persona_section(tmp_path):
    """Current marrow layout: user_name lives under [persona]."""
    marrow_cfg = tmp_path / "config.toml"
    marrow_cfg.write_text(
        """
[persona]
user_name = "小柚"
"""
    )
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    assert config.user_name(cfg) == "小柚"


def test_user_name_falls_back_to_legacy_top_level(tmp_path):
    """Old-layout marrow config: user_name at top level, no [persona] section."""
    marrow_cfg = tmp_path / "config.toml"
    marrow_cfg.write_text('user_name = "Legacy"\n')
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    assert config.user_name(cfg) == "Legacy"


def test_user_name_defaults_when_marrow_config_absent(tmp_path):
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "does_not_exist" / "marrow.db")
    assert config.user_name(cfg) == "the user"


# ── T6: shells single source (marrow [cortex].shells) ─────────────────────────

def _cfg_with_marrow_dir(tmp_path):
    cfg = config.load(tmp_path / "cortex.toml")
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    return cfg


def test_shells_defaults_to_cli_when_marrow_config_absent(tmp_path):
    cfg = _cfg_with_marrow_dir(tmp_path)
    assert config.shell_enabled(cfg) is True
    assert config.shell_enabled(cfg, "tg") is False


def test_shells_reads_from_marrow_config(tmp_path):
    (tmp_path / "config.toml").write_text('[cortex]\nshells = ["tg"]\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    assert config.shell_enabled(cfg) is False
    assert config.shell_enabled(cfg, "TG") is True


def test_away_idle_min_defaults_when_marrow_config_absent(tmp_path):
    cfg = _cfg_with_marrow_dir(tmp_path)
    assert config.away_idle_min(cfg) == 30


def test_away_idle_min_reads_from_marrow_config(tmp_path):
    (tmp_path / "config.toml").write_text('[cortex]\naway_idle_min = 35\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    assert config.away_idle_min(cfg) == 35


def test_away_idle_min_invalid_value_uses_default(tmp_path):
    (tmp_path / "config.toml").write_text('[cortex]\naway_idle_min = "later"\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    assert config.away_idle_min(cfg) == 30


def test_leftover_core_shells_key_warns_not_fatal(tmp_path, caplog):
    """cortex.toml [core].shells is no longer read; presence just warns once."""
    toml_path = tmp_path / "cortex.toml"
    toml_path.write_text('[core]\nshells = ["tg"]\n')
    with caplog.at_level("WARNING"):
        cfg = config.load(toml_path)
    assert any("[core].shells" in r.message for r in caplog.records)
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    # behaviour driven by marrow config only — the leftover key has no effect
    assert config.shell_enabled(cfg) is True


def test_wake_daemon_noops_when_cli_shell_off(tmp_path, monkeypatch, capsys):
    """Heartbeat entry exits before touching the DB or the lock when cli is not
    a shell in marrow's [cortex].shells."""
    from cortex import daemon

    (tmp_path / "config.toml").write_text('[cortex]\nshells = []\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    monkeypatch.setattr(daemon.config, "load", lambda: cfg)

    def _boom(*a, **kw):
        raise AssertionError("db.connect must not run with the cli shell off")

    monkeypatch.setattr(daemon.db, "connect", _boom)
    assert daemon.main([]) == 0
    assert "cli shell off" in capsys.readouterr().out


def test_watchdog_noops_when_cli_shell_off(tmp_path, monkeypatch):
    """Watchdog entry never writes its pidfile with the cli shell off in
    marrow's [cortex].shells."""
    from cortex import watchdog

    (tmp_path / "config.toml").write_text('[cortex]\nshells = []\n')
    cfg = _cfg_with_marrow_dir(tmp_path)
    monkeypatch.setattr(watchdog.config, "load", lambda: cfg)

    def _boom(*a, **kw):
        raise AssertionError("watchdog must not start with the cli shell off")

    monkeypatch.setattr(watchdog.wake_state, "watchdog_pidfile_path", _boom)
    assert watchdog.main([]) == 0


# --- silence bar: one default, three call sites -------------------------------

def _stub_silence_boundaries(monkeypatch, wake_state, transcript):
    monkeypatch.setattr(wake_state, "current_epoch", lambda c: (1, "sid"))
    monkeypatch.setattr(wake_state, "peek_kick_round", lambda c: False)
    monkeypatch.setattr(wake_state, "load", lambda c: {"awake": True})
    monkeypatch.setattr(wake_state, "silence_basis_min", lambda c, m: m)
    monkeypatch.setattr(wake_state, "conditional_mutate", lambda c, t, m: True)
    monkeypatch.setattr(transcript, "user_silent_min", lambda c: 0.0)


@pytest.mark.parametrize("bar", [55, 20, 37])
def test_silence_bar_resolves_from_one_default(tmp_path, monkeypatch, bar):
    """[wake.watchdog].silent_max_min is the single source: moving that default
    moves the note clamps, the daemon deadline and the watchdog gate together —
    no call site may carry its own fallback literal."""
    from cortex import daemon, transcript, wake_state, watchdog

    monkeypatch.setitem(config._DEFAULTS["wake"]["watchdog"], "silent_max_min", bar)
    cfg = config.load(tmp_path / "none.toml")
    assert cfg["wake"]["watchdog"]["silent_max_min"] == bar
    assert config.silent_max_min(cfg) == float(bar)

    assert config.wake_clamps(cfg)["silent_max_min"] == bar

    _stub_silence_boundaries(monkeypatch, wake_state, transcript)
    assert daemon.silence_due_in(cfg, {"awake": True}) == pytest.approx(bar * 60.0)

    assert watchdog.silence_action(cfg, bar - 1.0, allow_tuck=False) is None
    assert watchdog.silence_action(cfg, float(bar), allow_tuck=False) == \
        "free-round appended"


def test_watchdog_defaults_take_a_partial_user_block(tmp_path):
    """A user [wake.watchdog] setting only its own keys keeps the silence-bar
    default; setting the bar overrides it."""
    partial = tmp_path / "partial.toml"
    partial.write_text("[wake.watchdog]\npoll_sec = 5\nfuse_tokens = 123\n")
    cfg = config.load(partial)
    assert cfg["wake"]["watchdog"]["poll_sec"] == 5
    assert cfg["wake"]["watchdog"]["fuse_tokens"] == 123
    assert config.silent_max_min(cfg) == \
        float(config._DEFAULTS["wake"]["watchdog"]["silent_max_min"])

    override = tmp_path / "override.toml"
    override.write_text("[wake.watchdog]\nsilent_max_min = 7\n")
    cfg = config.load(override)
    assert config.silent_max_min(cfg) == 7.0
    assert config.wake_clamps(cfg)["silent_max_min"] == 7


def test_default_sleep_min_resolves_from_the_default(tmp_path, monkeypatch):
    """occupancy.schedule_next_wake reads [wake].default_sleep_min through the
    same single default — no duplicate literal."""
    from datetime import datetime, timedelta

    from cortex import occupancy

    monkeypatch.setitem(config._DEFAULTS["wake"], "default_sleep_min", 33)
    cfg = config.load(tmp_path / "none.toml")
    now = datetime(2026, 1, 1, 12, 0)
    assert occupancy.schedule_next_wake(now, cfg) == now + timedelta(minutes=33)
    assert occupancy.schedule_next_wake(now, {}) == now + timedelta(minutes=33)
