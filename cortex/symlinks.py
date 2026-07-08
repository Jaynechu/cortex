"""One-time idempotent symlinks: day_log.md + wishlist.md into NY db-pages
(Decided 07-03 eve). Fixed source paths + archive-move-recreate on day_log
keep the link zero-maintenance. Never touches any other NY file. Also installs
the cortex-only slash commands (say / lie-down) into <home>/.claude/commands/ so
they are self-discoverable inside the cortex window (and invisible elsewhere).
"""
from __future__ import annotations

from pathlib import Path

from cortex import config

WISHLIST_HEADER = "# Wishlist\n\n(owed treats / her wants / her self-rewards — append-only)\n"

# Repo source of truth for the cortex-home slash commands (project-scoped: only
# a session whose cwd is cortex_home sees them).
_COMMANDS_SRC = Path(__file__).resolve().parent.parent / "deploy" / "commands"


def ensure_wishlist(path: Path) -> None:
    """Create wishlist.md with a minimal header if missing. Never overwrites
    an existing file (pure md, one-way, her hand edits are the source of truth)."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WISHLIST_HEADER)


def _ensure_symlink(source: Path, target: Path) -> None:
    if target.is_symlink():
        if target.resolve() == source.resolve():
            return
        raise FileExistsError(f"{target} is a symlink to something else: {target.resolve()}")
    if target.exists():
        raise FileExistsError(f"{target} exists and is not a symlink — refusing to clobber")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source)


def ensure_commands(cfg: dict) -> None:
    """Symlink each deploy/commands/*.md into <cortex_home>/.claude/commands/.
    Project-scoped commands: only visible to a session with that cwd (the cortex
    window), never to normal sessions. Refuses to clobber a real file."""
    if not _COMMANDS_SRC.is_dir():
        return
    dest_dir = config.cortex_home(cfg) / ".claude" / "commands"
    for src in sorted(_COMMANDS_SRC.glob("*.md")):
        _ensure_symlink(src, dest_dir / src.name)


def ensure_all(cfg: dict) -> None:
    """Idempotent: safe to call on every wake."""
    ny_dir = config.ny_db_pages_dir(cfg)
    day_log_source = config.day_log_path(cfg)
    wishlist_source = config.wishlist_path(cfg)

    ensure_wishlist(wishlist_source)
    _ensure_symlink(day_log_source, ny_dir / "day_log.md")
    _ensure_symlink(wishlist_source, ny_dir / "wishlist.md")
    ensure_commands(cfg)
