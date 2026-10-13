"""Where dj-setbox keeps its files by default.

Running from a source checkout (pyproject.toml two levels above the package): next to the repo,
so existing setups keep `./backups` and `./data`. Installed as a package: `~/.dj-setbox/`.
Both are overridable with REKORDBOX_BACKUP_DIR and RBMCP_ENGINE_DB.
"""
from __future__ import annotations

from pathlib import Path

_PKG = Path(__file__).resolve().parent


def home() -> Path:
    repo = _PKG.parents[1]
    if (repo / "pyproject.toml").exists() and (repo / "src").is_dir():
        return repo
    return Path.home() / ".dj-setbox"


def backups() -> Path:
    return home() / "backups"


def engine_db() -> Path:
    return home() / "data" / "engine.db"
