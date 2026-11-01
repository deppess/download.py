"""Read-only environment checks for the safe music pipeline."""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import sys
from pathlib import Path

from music_paths import config_path, load_paths


def _status(label: str, result: str) -> None:
    print(f"{label:<20} {result}")


def _managed_media_path(path: Path) -> bool:
    return "media.localized" in {part.casefold() for part in path.parts}


def run() -> int:
    try:
        paths = load_paths()
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    failures = 0
    warnings = 0
    print("Music pipeline doctor\n")
    _status(
        "Config",
        str(config_path())
        if config_path().is_file()
        else "not present (defaults/legacy INI active)",
    )
    for label, path in (
        ("Music root", paths.root),
        ("Library", paths.library),
        ("Staging", paths.landing),
        ("State", paths.state),
    ):
        if _managed_media_path(path):
            _status(label, f"ERROR: managed-media path is forbidden: {path}")
            failures += 1
        else:
            _status(label, str(path))
    if paths.library == paths.landing or paths.library in paths.landing.parents:
        _status("Path layout", "ERROR: library and staging overlap")
        failures += 1
    else:
        _status("Path layout", "OK: staging is separate from library")
    for command in ("yt-dlp", "ffmpeg"):
        location = shutil.which(command)
        if location:
            _status(command, location)
        else:
            _status(command, "ERROR: not found on PATH")
            failures += 1
    for package in ("mutagen", "pylast", "PIL"):
        if importlib.util.find_spec(package):
            _status(f"Python {package}", "available")
        else:
            _status(f"Python {package}", "WARNING: feature unavailable")
            warnings += 1
    if paths.library.exists():
        _status("Library directory", "present")
    else:
        _status("Library directory", "WARNING: not created yet")
        warnings += 1
    if paths.landing.exists():
        _status("Staging directory", "present")
    else:
        _status("Staging directory", "will be created on fetch")
    print(f"\nResult: failures={failures}, warnings={warnings}")
    return 2 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only music-pipeline environment checks.")
    parser.parse_args(argv)
    return run()
