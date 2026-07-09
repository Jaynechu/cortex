"""One-time idempotent symlinks: day_log.md + wishlist.md into NY db-pages
(Decided 07-03 eve). Fixed source paths + archive-move-recreate on day_log
keep the link zero-maintenance. Never touches any other NY file.
"""
from __future__ import annotations

from pathlib import Path

from cortex import config


def ensure_wishlist(path: Path, header: str) -> None:
    """Create wishlist.md with a minimal header if missing. Never overwrites
    an existing file (pure md, one-way, hand edits are the source of truth)."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header)


def _ensure_symlink(source: Path, target: Path) -> None:
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return
        raise FileExistsError(f"{target} is a symlink to something else: {target.resolve()}")
    if target.exists():
        raise FileExistsError(f"{target} exists and is not a symlink — refusing to clobber")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source)


def ensure_all(cfg: dict) -> None:
    """Idempotent: safe to call on every wake."""
    ny_dir = config.ny_db_pages_dir(cfg)
    day_log_source = config.day_log_path(cfg)
    wishlist_source = config.wishlist_path(cfg)

    ensure_wishlist(wishlist_source, config.wishlist_header(cfg))
    _ensure_symlink(day_log_source, ny_dir / "day_log.md")
    _ensure_symlink(wishlist_source, ny_dir / "wishlist.md")
