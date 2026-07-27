"""note_render --shell <non-cli>: per-shell replay cursor + out-of-band cutoff.

The cli render is the regression baseline — it must stay byte-identical (cursor
from wake_state, stdout only). A non-cli shell diffs from its own ledger and
reports the rendered cutoff on stderr; it writes nothing anywhere.
"""
from __future__ import annotations

import json

import pytest

from cortex import config, db, note, note_render, wake_state
from tests.test_note import _row_id, make_events_table

SHELL = "tg"


def _snapshot(*paths):
    """bytes-or-None per path, so 'no write' covers 'file never created' too."""
    return [p.read_bytes() if p.exists() else None for p in paths]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fully tmp-scoped cfg + an events table, with config.load patched so
    note_render.main() picks it up instead of the live cortex.toml."""
    cfg = config.load(path=tmp_path / "absent.toml")
    cfg["note"]["wake_machine_tag"] = ""
    cfg["paths"]["marrow_db"] = str(tmp_path / "marrow.db")
    cfg["paths"]["cortex_home"] = str(tmp_path / "home")
    cfg["paths"]["wake_state_file"] = str(tmp_path / "wake_state.json")
    cfg["paths"]["shell_state_dir"] = str(tmp_path / "shells")
    (tmp_path / "shells").mkdir()
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
    ids = {c: _row_id(conn, c) for c in ("first", "second", "third")}
    conn.close()
    return {
        "cfg": cfg,
        "ids": ids,
        "ledger": tmp_path / "shells" / f"{SHELL}.json",
        "wake_state": tmp_path / "wake_state.json",
    }


def _run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["note_render", *argv])
    note_render.main()


def test_shell_render_uses_ledger_cursor_and_reports_cutoff(env, monkeypatch, capsys):
    """--shell tg diffs from the tg ledger's last_note_row_id, prints only the
    newer rows on stdout, and reports the rendered cutoff on stderr — writing
    neither the ledger nor wake_state."""
    env["ledger"].write_text(json.dumps({"last_note_row_id": env["ids"]["first"]}))
    before = _snapshot(env["ledger"], env["wake_state"])

    _run(monkeypatch, "--shell", SHELL)
    out, err = capsys.readouterr()

    assert "first" not in out
    assert "second" in out and "third" in out
    assert err.strip() == f"cutoff_row_id={env['ids']['third']}"
    assert _snapshot(env["ledger"], env["wake_state"]) == before


def test_shell_render_no_eligible_rows_emits_no_cutoff(env, monkeypatch, capsys):
    """Cursor already at the newest row: nothing to replay -> no cutoff line at
    all (the feeder must promote nothing), and still no writes."""
    env["ledger"].write_text(json.dumps({"last_note_row_id": env["ids"]["third"]}))
    before = _snapshot(env["ledger"], env["wake_state"])

    _run(monkeypatch, "--shell", SHELL)
    out, err = capsys.readouterr()

    assert "cutoff_row_id" not in err
    assert "third" not in out
    assert _snapshot(env["ledger"], env["wake_state"]) == before


def test_shell_render_missing_ledger_is_full_window(env, monkeypatch, capsys):
    """No ledger file yet (first tg render): full window, same as a cli first
    read — and the render still creates nothing."""
    assert not env["ledger"].exists()
    before = _snapshot(env["ledger"], env["wake_state"])

    _run(monkeypatch, "--shell", SHELL)
    out, err = capsys.readouterr()

    assert "first" in out and "second" in out and "third" in out
    assert err.strip() == f"cutoff_row_id={env['ids']['third']}"
    assert _snapshot(env["ledger"], env["wake_state"]) == before


def test_shell_render_ignores_wake_state_cursor(env, monkeypatch, capsys):
    """wake_state belongs to the cli shell: a tg render must not consume it, so a
    caught-up cli cursor never hides rows from tg."""
    wake_state.set_last_note_row_id(env["cfg"], env["ids"]["third"])
    env["ledger"].write_text(json.dumps({"last_note_row_id": env["ids"]["second"]}))

    _run(monkeypatch, "--shell", SHELL)
    out, err = capsys.readouterr()

    assert "third" in out and "second" not in out
    assert err.strip() == f"cutoff_row_id={env['ids']['third']}"


def test_unqualified_render_unchanged(env, monkeypatch, capsys):
    """Baseline: no --shell -> cursor from wake_state, ledger ignored, still no
    writes. The cutoff IS reported (the marrow wake hook advances wake_state
    with it after injecting), the render itself just never writes one."""
    wake_state.set_last_note_row_id(env["cfg"], env["ids"]["second"])
    env["ledger"].write_text(json.dumps({"last_note_row_id": env["ids"]["first"]}))
    before = _snapshot(env["ledger"], env["wake_state"])

    _run(monkeypatch)
    out, err = capsys.readouterr()

    assert "third" in out
    assert "second" not in out and "first" not in out
    assert err.strip() == f"cutoff_row_id={env['ids']['third']}"
    assert _snapshot(env["ledger"], env["wake_state"]) == before


def test_cli_shell_render_reports_cutoff_without_writing(env, monkeypatch, capsys):
    """--shell cli is the unqualified path: wake_state cursor, cutoff on stderr,
    no cursor write of its own."""
    wake_state.set_last_note_row_id(env["cfg"], env["ids"]["second"])
    env["ledger"].write_text(json.dumps({"last_note_row_id": env["ids"]["first"]}))
    before = _snapshot(env["ledger"], env["wake_state"])

    _run(monkeypatch, "--shell", note.CLI_SHELL)
    out, err = capsys.readouterr()

    assert "third" in out and "second" not in out
    assert err.strip() == f"cutoff_row_id={env['ids']['third']}"
    assert _snapshot(env["ledger"], env["wake_state"]) == before
