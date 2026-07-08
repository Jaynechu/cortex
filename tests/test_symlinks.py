from __future__ import annotations

from pathlib import Path

import pytest

from cortex import symlinks


@pytest.fixture
def scfg(tmp_path):
    return {
        "paths": {
            "day_log": str(tmp_path / "day_log.md"),
            "wishlist_file": str(tmp_path / "cortex_home" / "wishlist.md"),
            "ny_db_pages": str(tmp_path / "ny"),
            "cortex_home": str(tmp_path / "cortex_home"),
        }
    }


def test_ensure_wishlist_creates_minimal_header(tmp_path):
    path = tmp_path / "wishlist.md"
    symlinks.ensure_wishlist(path)
    assert path.exists()
    assert "# Wishlist" in path.read_text()


def test_ensure_wishlist_never_overwrites_existing(tmp_path):
    path = tmp_path / "wishlist.md"
    path.write_text("her own content\n")
    symlinks.ensure_wishlist(path)
    assert path.read_text() == "her own content\n"


def test_ensure_all_creates_both_symlinks(scfg, tmp_path):
    (tmp_path / "day_log.md").write_text("2026-07-03\n")
    symlinks.ensure_all(scfg)

    ny = Path(scfg["paths"]["ny_db_pages"])
    assert (ny / "day_log.md").is_symlink()
    assert (ny / "day_log.md").resolve() == (tmp_path / "day_log.md").resolve()
    assert (ny / "wishlist.md").is_symlink()
    assert Path(scfg["paths"]["wishlist_file"]).exists()


def test_ensure_all_idempotent(scfg, tmp_path):
    (tmp_path / "day_log.md").write_text("2026-07-03\n")
    symlinks.ensure_all(scfg)
    symlinks.ensure_all(scfg)  # second call: no error, no-op

    ny = Path(scfg["paths"]["ny_db_pages"])
    assert (ny / "day_log.md").is_symlink()


def test_ensure_all_refuses_to_clobber_foreign_file(scfg, tmp_path):
    ny = Path(scfg["paths"]["ny_db_pages"])
    ny.mkdir(parents=True)
    (ny / "day_log.md").write_text("her own unrelated file\n")

    with pytest.raises(FileExistsError):
        symlinks.ensure_all(scfg)

    assert (ny / "day_log.md").read_text() == "her own unrelated file\n"


def test_ensure_commands_lands_into_home(scfg, tmp_path):
    symlinks.ensure_commands(scfg)
    cmd_dir = Path(scfg["paths"]["cortex_home"]) / ".claude" / "commands"
    names = {p.name for p in cmd_dir.glob("*.md")}
    assert {"say.md", "lie-down.md"} <= names
    for p in cmd_dir.glob("*.md"):
        assert p.read_text().startswith("---")  # valid frontmatter


def test_ensure_commands_templated_copy_resolves_token(scfg):
    """say/lie-down carry __VENV_PYTHON__ in the repo (no personal path); they
    land as token-resolved COPIES, not symlinks."""
    from cortex.install import venv_python

    symlinks.ensure_commands(scfg)
    cmd_dir = Path(scfg["paths"]["cortex_home"]) / ".claude" / "commands"
    for name in ("say.md", "lie-down.md"):
        p = cmd_dir / name
        assert not p.is_symlink()
        body = p.read_text()
        assert "__VENV_PYTHON__" not in body
        assert str(venv_python()) in body


def test_ensure_commands_migrates_legacy_symlink(scfg):
    """A pre-existing symlink (old behaviour) is replaced by a resolved copy."""
    symlinks.ensure_commands(scfg)  # first: real copies
    cmd_dir = Path(scfg["paths"]["cortex_home"]) / ".claude" / "commands"
    say = cmd_dir / "say.md"
    say.unlink()
    say.symlink_to(symlinks._COMMANDS_SRC / "say.md")  # simulate legacy symlink
    assert say.is_symlink()
    symlinks.ensure_commands(scfg)
    assert not say.is_symlink()
    assert "__VENV_PYTHON__" not in say.read_text()


def test_ensure_commands_refuses_foreign_command_file(scfg):
    cmd_dir = Path(scfg["paths"]["cortex_home"]) / ".claude" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "say.md").write_text("her own unrelated command\n")
    with pytest.raises(FileExistsError):
        symlinks.ensure_commands(scfg)


def test_ensure_commands_idempotent(scfg):
    symlinks.ensure_commands(scfg)
    symlinks.ensure_commands(scfg)  # no error on second run
