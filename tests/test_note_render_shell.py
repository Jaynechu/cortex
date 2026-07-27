"""note_render --shell/--replay: private per-shell replay marker.

The tg shell is a headless consumer — no UserPromptSubmit hook, so the note is
its only replay outlet and this render consumes its marker. Every window render
(no --replay) carries no Replay section and touches no marker.
"""
from __future__ import annotations

import fcntl
import os

import pytest

from cortex import config, db, note, note_render
from tests.test_note import make_events_table

SHELL = "tg"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fully tmp-scoped cfg + an events table, with config.load patched so
    note_render.main() picks it up instead of the live cortex.toml."""
    cfg = config.load(path=tmp_path / "absent.toml")
    cfg["note"]["wake_machine_tag"] = ""
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    cfg["paths"]["cortex_home"] = str(tmp_path / "home")
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    monkeypatch.setattr(config, "load", lambda *a, **kw: cfg)
    monkeypatch.setattr(note, "_frontmost_app", lambda: None)

    conn = db.connect(cfg)
    make_events_table(conn)
    for i, content in enumerate(("first", "second", "third")):
        conn.execute(
            "INSERT INTO events (session_id, timestamp, role, content, channel)"
            " VALUES (?,?,?,?,?)",
            ("s", f"2026-07-08T03:0{i}:00+00:00", "user", content, "wx"))
    conn.commit()
    conn.close()
    return cfg


def _run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["note_render", *argv])
    note_render.main()


def _marker(cfg, shell):
    p = note._marker_path(cfg, shell)
    return int(p.read_text()) if p.exists() else None


def test_first_shell_render_seeds_and_shows_the_window(env, monkeypatch, capsys):
    """No marker yet: the latest window renders and the marker records it."""
    _run(monkeypatch, "--shell", SHELL, "--replay")
    out = capsys.readouterr().out
    assert "first" in out and "second" in out and "third" in out
    assert _marker(env, SHELL) == 3


def test_second_render_with_no_new_rows_shows_nothing(env, monkeypatch, capsys):
    _run(monkeypatch, "--shell", SHELL, "--replay")
    capsys.readouterr()
    _run(monkeypatch, "--shell", SHELL, "--replay")
    out = capsys.readouterr().out
    assert "### Replay" not in out
    assert _marker(env, SHELL) == 3


def test_only_rows_newer_than_the_marker_render(env, monkeypatch, capsys):
    _run(monkeypatch, "--shell", SHELL, "--replay")
    capsys.readouterr()
    conn = db.connect(env)
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel)"
        " VALUES ('s','2026-07-08T03:09:00+00:00','user','fourth','wx')")
    conn.commit()
    conn.close()
    _run(monkeypatch, "--shell", SHELL, "--replay")
    out = capsys.readouterr().out
    assert "fourth" in out
    assert "third" not in out and "first" not in out


def test_shell_render_advances_only_its_own_marker(env, monkeypatch, capsys):
    """The tg render must not consume the cli shell's window, or vice versa."""
    _run(monkeypatch, "--shell", SHELL, "--replay")
    capsys.readouterr()
    assert _marker(env, SHELL) == 3
    assert _marker(env, note.CLI_SHELL) is None

    _run(monkeypatch, "--shell", note.CLI_SHELL, "--replay")
    out = capsys.readouterr().out
    assert "third" in out          # cli has its own, untouched position
    assert _marker(env, note.CLI_SHELL) == 3


def test_shell_render_drops_its_own_channel(env, monkeypatch, capsys):
    conn = db.connect(env)
    conn.execute(
        "INSERT INTO events (session_id, timestamp, role, content, channel)"
        " VALUES ('s','2026-07-08T03:09:00+00:00','assistant','tg self talk','tg')")
    conn.commit()
    conn.close()
    _run(monkeypatch, "--shell", SHELL, "--replay")
    out = capsys.readouterr().out
    assert "tg self talk" not in out
    assert "third" in out


def test_window_render_carries_no_replay_and_no_marker(env, monkeypatch, capsys):
    """No --replay (the marrow render_module window path): no Replay section and
    no marker write, so a window render can never poison a headless consumer."""
    _run(monkeypatch)
    out = capsys.readouterr().out
    assert "### Replay" not in out
    assert "third" not in out
    assert _marker(env, note.CLI_SHELL) is None


def test_busy_marker_lock_skips_the_round_without_writing(env, monkeypatch, capsys):
    p = note._marker_path(env, SHELL)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        _run(monkeypatch, "--shell", SHELL, "--replay")
        out = capsys.readouterr().out
        assert "### Replay" not in out
        assert _marker(env, SHELL) is None
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # released -> the same window is still there to show
    _run(monkeypatch, "--shell", SHELL, "--replay")
    assert "third" in capsys.readouterr().out
