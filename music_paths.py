"""Single source of truth for music-pipeline filesystem locations.

Set ``DOWNLOAD_MUSIC_ROOT`` to use a different root in tests or on another
machine. No location in this module is an Apple Music managed-media folder.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MusicPaths:
    root: Path
    library: Path
    landing: Path
    state: Path
    lyrics: Path
    archive: Path
    failed: Path


def config_path() -> Path:
    """Return the optional TOML config location without requiring it to exist."""
    return Path(os.environ.get("DOWNLOAD_CONFIG", "~/.config/download.py/config.toml")).expanduser()


def _configured_root(path: Path) -> Path | None:
    if not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        value = data.get("paths", {}).get("root")
    except (tomllib.TOMLDecodeError, OSError, AttributeError) as exc:
        raise ValueError(f"Invalid configuration file {path}: {exc}") from exc
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[paths].root must be a non-empty string in {path}")
    return Path(value).expanduser()


def load_paths() -> MusicPaths:
    default = (
        Path.home() / "Music" if sys.platform == "darwin" else Path("/home/deppes/Media/music")
    )
    root = Path(
        os.environ.get("DOWNLOAD_MUSIC_ROOT") or _configured_root(config_path()) or default
    ).expanduser()
    return MusicPaths(
        root=root,
        library=root / "Library",
        landing=root / "download-staging",
        state=root / ".download-state",
        lyrics=root / "lyrics",
        archive=root / ".downloaded.txt",
        failed=root / ".failed.txt",
    )
