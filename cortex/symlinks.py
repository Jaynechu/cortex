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


def _ensure_templated_copy(source: Path, target: Path) -> None:
    """Land a token-resolved COPY (not a symlink): the command body carries the
    repo placeholder __VENV_PYTHON__ (no personal path in the OSS repo), resolved
    to the real interpreter here. Refuses to clobber a real (non-managed) file;
    rewrites the copy in place so template edits propagate on the next wake."""
    from cortex.install import venv_python

    body = source.read_text().replace("__VENV_PYTHON__", str(venv_python()))
    if target.is_symlink():
        target.unlink()  # migrate a legacy symlink to a real copy
    elif target.exists() and "__VENV_PYTHON__" not in target.read_text() \
            and str(venv_python()) not in target.read_text():
        raise FileExistsError(f"{target} exists and is not a managed copy — refusing to clobber")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


def ensure_commands(cfg: dict) -> None:
    """Land each deploy/commands/*.md into <cortex_home>/.claude/commands/.
    Project-scoped commands: only visible to a session with that cwd (the cortex
    window), never to normal sessions. Templated bodies (__VENV_PYTHON__) land as
    token-resolved copies so no personal path lives in the repo; the rest symlink.
    Refuses to clobber a real file."""
    if not _COMMANDS_SRC.is_dir():
        return
    dest_dir = config.cortex_home(cfg) / ".claude" / "commands"
    for src in sorted(_COMMANDS_SRC.glob("*.md")):
        dest = dest_dir / src.name
        if "__VENV_PYTHON__" in src.read_text():
            _ensure_templated_copy(src, dest)
        else:
            _ensure_symlink(src, dest)


def ensure_all(cfg: dict) -> None:
    """Idempotent: safe to call on every wake."""
    ny_dir = config.ny_db_pages_dir(cfg)
    day_log_source = config.day_log_path(cfg)
    wishlist_source = config.wishlist_path(cfg)

    ensure_wishlist(wishlist_source)
    _ensure_symlink(day_log_source, ny_dir / "day_log.md")
    _ensure_symlink(wishlist_source, ny_dir / "wishlist.md")
    ensure_commands(cfg)
