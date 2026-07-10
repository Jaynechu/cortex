from __future__ import annotations

from pathlib import Path

import pytest

from cortex import symlinks


@pytest.fixture
def scfg(tmp_path):
    return {
        "paths": {
            "daybrief": str(tmp_path / "daybrief.md"),
            "wishlist_file": str(tmp_path / "cortex_home" / "wishlist.md"),
            "ny_db_pages": str(tmp_path / "ny"),
            "cortex_home": str(tmp_path / "cortex_home"),
        }
    }


def test_ensure_wishlist_creates_minimal_header(tmp_path):
    path = tmp_path / "wishlist.md"
    symlinks.ensure_wishlist(path, "# Wishlist\n")
    assert path.exists()
    assert "# Wishlist" in path.read_text()


def test_ensure_wishlist_never_overwrites_existing(tmp_path):
    path = tmp_path / "wishlist.md"
    path.write_text("existing content\n")
    symlinks.ensure_wishlist(path, "# Wishlist\n")
    assert path.read_text() == "existing content\n"


def test_ensure_all_creates_both_symlinks(scfg, tmp_path):
    (tmp_path / "daybrief.md").write_text("2026-07-03\n")
    symlinks.ensure_all(scfg)

    ny = Path(scfg["paths"]["ny_db_pages"])
    assert (ny / "daybrief.md").is_symlink()
    assert (ny / "daybrief.md").resolve() == (tmp_path / "daybrief.md").resolve()
    assert (ny / "wishlist.md").is_symlink()
    assert Path(scfg["paths"]["wishlist_file"]).exists()


def test_ensure_all_idempotent(scfg, tmp_path):
    (tmp_path / "daybrief.md").write_text("2026-07-03\n")
    symlinks.ensure_all(scfg)
    symlinks.ensure_all(scfg)  # second call: no error, no-op

    ny = Path(scfg["paths"]["ny_db_pages"])
    assert (ny / "daybrief.md").is_symlink()


def test_ensure_all_refuses_to_clobber_foreign_file(scfg, tmp_path):
    ny = Path(scfg["paths"]["ny_db_pages"])
    ny.mkdir(parents=True)
    (ny / "daybrief.md").write_text("her own unrelated file\n")

    with pytest.raises(FileExistsError):
        symlinks.ensure_all(scfg)

    assert (ny / "daybrief.md").read_text() == "her own unrelated file\n"
