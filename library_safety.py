"""Non-destructive inventory, backup, and verification for a music library.

This module deliberately has no delete, move, rename-source, or tag-writing
operation.  It is the safety gate for a later library rebuild.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from music_paths import load_paths

try:
    from mutagen.mp4 import MP4
except ImportError:  # pragma: no cover - the main program reports dependencies
    MP4 = None


DEFAULT_SOURCE = Path.home() / "Music" / "Music" / "Media.localized" / "Music"
DEFAULT_BACKUP = Path.home() / "Music" / "Library-Backup"
DEFAULT_MUSIC_DATABASE = Path.home() / "Music" / "Music" / "Music Library.musiclibrary"
MANIFEST_NAME = "library-manifest.jsonl"
MANIFEST_META_NAME = "library-manifest-meta.json"
CHUNK_SIZE = 1024 * 1024


class SafetyError(RuntimeError):
    """Raised when a requested operation cannot safely continue."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_tag(tags, key: str) -> str:
    values = (tags or {}).get(key, [""])
    return str(values[0]).strip() if values else ""


def inspect_file(path: Path, root: Path, include_hash: bool = True) -> dict:
    """Return a stable, JSON-serializable inventory record for one M4A file."""
    record = {
        "relative_path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": sha256(path) if include_hash else None,
        "readable": False,
        "duration": None,
        "artist": "",
        "title": "",
        "album": "",
        "genre": "",
        "source_url": "",
        "error": None,
    }
    if MP4 is None:
        record["error"] = "mutagen is not installed"
        return record
    try:
        audio = MP4(path)
        tags = audio.tags or {}
        record.update(
            {
                "readable": True,
                "duration": round(float(audio.info.length), 3) if audio.info else None,
                "artist": _first_tag(tags, "\xa9ART"),
                "title": _first_tag(tags, "\xa9nam"),
                "album": _first_tag(tags, "\xa9alb"),
                "genre": _first_tag(tags, "\xa9gen"),
                "source_url": _first_tag(tags, "\xa9cmt"),
            }
        )
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def inventory(root: Path, include_hash: bool = True) -> list[dict]:
    if not root.is_dir():
        raise SafetyError(f"Library directory does not exist: {root}")
    return [inspect_file(path, root, include_hash) for path in sorted(root.rglob("*.m4a"))]


def inventory_files(root: Path) -> list[dict]:
    """Hash every ordinary file in a package such as Music Library.musiclibrary."""
    if not root.is_dir():
        raise SafetyError(f"Directory does not exist: {root}")
    return [
        {
            "relative_path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted(item for item in root.rglob("*") if item.is_file())
    ]


def write_manifest(root: Path, records: list[dict], destination: Path | None = None) -> Path:
    destination = destination or root
    destination.mkdir(parents=True, exist_ok=True)
    manifest = destination / MANIFEST_NAME
    temporary = destination / f".{MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(manifest)
    meta = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_root": str(root),
        "file_count": len(records),
        "total_bytes": sum(record["size"] for record in records),
        "all_readable": all(record["readable"] for record in records),
    }
    (destination / MANIFEST_META_NAME).write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_manifest(path: Path) -> list[dict]:
    if not path.is_file():
        raise SafetyError(f"Manifest does not exist: {path}")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def music_is_running() -> bool:
    """Avoid taking a supposedly stable backup while Music can alter its files."""
    if sys.platform != "darwin":
        return False
    result = subprocess.run(
        ["pgrep", "-x", "Music"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def require_safe_backup_preconditions(source: Path, destination: Path) -> None:
    if music_is_running():
        raise SafetyError("Quit Apple Music before creating a backup, then run this command again.")
    if not source.is_dir():
        raise SafetyError(f"Source library does not exist: {source}")
    if destination.exists():
        raise SafetyError(
            f"Backup destination already exists (refusing to overwrite): {destination}"
        )
    if destination.resolve().is_relative_to(source.resolve()):
        raise SafetyError("Backup destination cannot be inside the source library.")
    source_size = sum(path.stat().st_size for path in source.rglob("*.m4a"))
    free_space = shutil.disk_usage(destination.parent).free
    # The manifest and filesystem overhead mean exactly enough space is not enough.
    if free_space < source_size * 11 // 10:
        raise SafetyError(
            f"Insufficient free space: need at least {source_size * 11 // 10} bytes; have {free_space} bytes."
        )


def atomic_verified_copy(source: Path, destination: Path, expected_hash: str) -> None:
    """Copy to a sibling temp file, hash it, then atomically publish it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    try:
        shutil.copy2(source, temporary)
        actual_hash = sha256(temporary)
        if actual_hash != expected_hash:
            raise SafetyError(f"Checksum mismatch while copying {source}")
        if destination.exists():
            raise SafetyError(f"Refusing to overwrite destination: {destination}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _copy_package(source: Path, destination: Path) -> tuple[int, int]:
    """Copy and hash-verify every file in an application package."""
    records = inventory_files(source)
    for record in records:
        atomic_verified_copy(
            source / record["relative_path"],
            destination / record["relative_path"],
            record["sha256"],
        )
    manifest = destination.parent / f"{destination.name}.manifest.jsonl"
    with manifest.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return len(records), sum(record["size"] for record in records)


def create_backup(
    source: Path, destination: Path, music_database: Path | None = None
) -> tuple[int, int]:
    require_safe_backup_preconditions(source, destination)
    records = inventory(source, include_hash=True)
    bad = [record for record in records if not record["readable"]]
    if bad:
        raise SafetyError(
            f"Refusing backup: {len(bad)} source files cannot be read. Run audit for details."
        )

    destination.mkdir(parents=True)
    try:
        for number, record in enumerate(records, 1):
            source_file = source / record["relative_path"]
            backup_file = destination / record["relative_path"]
            atomic_verified_copy(source_file, backup_file, record["sha256"])
            print(f"  [{number:>4}/{len(records)}] backed up {record['relative_path']}")
        manifest = write_manifest(source, records, destination)
        errors = verify_manifest(destination, manifest)
        if errors:
            raise SafetyError(
                f"Backup verification failed with {len(errors)} error(s): {errors[0]}"
            )
        if music_database:
            if not music_database.is_dir():
                raise SafetyError(f"Music database does not exist: {music_database}")
            count, size = _copy_package(music_database, destination / music_database.name)
            print(f"  Backed up Music database: {count} files, {size / 1024**2:.1f} MiB")
    except Exception:  # noqa: TRY203
        # Deliberately leave partial backup data in place for inspection. Never
        # remove data after a failure, and never touch the source library.
        raise
    return len(records), sum(record["size"] for record in records)


def verify_manifest(root: Path, manifest_path: Path) -> list[str]:
    """Verify every manifest item; return all errors without changing files."""
    errors: list[str] = []
    records = load_manifest(manifest_path)
    expected_paths = {record["relative_path"] for record in records}
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*.m4a")}
    for missing in sorted(expected_paths - actual_paths):
        errors.append(f"missing: {missing}")
    for extra in sorted(actual_paths - expected_paths):
        errors.append(f"unexpected: {extra}")
    for record in records:
        path = root / record["relative_path"]
        if not path.is_file():
            continue
        if path.stat().st_size != record["size"]:
            errors.append(f"size mismatch: {record['relative_path']}")
            continue
        if sha256(path) != record["sha256"]:
            errors.append(f"checksum mismatch: {record['relative_path']}")
            continue
        checked = inspect_file(path, root, include_hash=False)
        if not checked["readable"]:
            errors.append(f"unreadable: {record['relative_path']}: {checked['error']}")
    return errors


def _print_audit(records: list[dict]) -> None:
    unreadable = [record for record in records if not record["readable"]]
    missing_identity = [record for record in records if not record["artist"] or not record["title"]]
    missing_source = [record for record in records if not record["source_url"]]
    print(f"Files:            {len(records)}")
    print(f"Total size:       {sum(record['size'] for record in records) / 1024**3:.2f} GiB")
    print(f"Readable:         {len(records) - len(unreadable)}")
    print(f"Unreadable:       {len(unreadable)}")
    print(f"Missing identity: {len(missing_identity)}")
    print(f"Missing source:   {len(missing_source)}")
    for record in unreadable:
        print(f"  ERROR {record['relative_path']}: {record['error']}")


def main(argv: list[str] | None = None) -> int:
    paths = load_paths()
    # Kept separate from backup operations: rebuild planning has no access to
    # copy/move/delete functions and only creates state/report files.
    if argv and argv[0] == "rebuild":
        from library_rebuild import main as rebuild_main

        return rebuild_main(argv[1:])
    parser = argparse.ArgumentParser(description="Safe, non-destructive music-library preparation.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit_parser = subparsers.add_parser("audit", help="Read-only library inventory.")
    audit_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    audit_parser.add_argument("--write-manifest", action="store_true")
    backup_parser = subparsers.add_parser(
        "backup", help="Verified copy; never overwrites or removes files."
    )
    backup_parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    backup_parser.add_argument("--destination", type=Path, default=DEFAULT_BACKUP)
    backup_parser.add_argument("--music-database", type=Path, default=DEFAULT_MUSIC_DATABASE)
    verify_parser = subparsers.add_parser(
        "verify", help="Read-only verification of a manifest and directory."
    )
    verify_parser.add_argument("--root", type=Path, required=True)
    verify_parser.add_argument("--manifest", type=Path, required=True)
    adopt_parser = subparsers.add_parser(
        "adopt", help="Validate and register an existing finished library; never changes media."
    )
    adopt_parser.add_argument("--source", type=Path, default=paths.library)
    adopt_parser.add_argument(
        "--manifest",
        type=Path,
        default=paths.library / ".sorted-library-manifest.jsonl",
    )
    adopt_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    status_parser = subparsers.add_parser("status", help="Read-only summary of pipeline state.")
    status_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    cache_parser = subparsers.add_parser(
        "migrate-lastfm-cache",
        help="Copy legacy Last.fm evidence into shared state; no network or media changes.",
    )
    cache_parser.add_argument(
        "--source",
        type=Path,
        default=paths.state / "rebuild-evidence.sqlite3",
    )
    cache_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            records = inventory(args.source, include_hash=args.write_manifest)
            _print_audit(records)
            if args.write_manifest:
                print(f"Manifest: {write_manifest(args.source, records)}")
            return 1 if any(not record["readable"] for record in records) else 0
        if args.command == "backup":
            count, size = create_backup(args.source, args.destination, args.music_database)
            print(f"Verified backup complete: {count} files, {size / 1024**3:.2f} GiB")
            print(f"Manifest: {args.destination / MANIFEST_NAME}")
            return 0
        if args.command == "adopt":
            from pipeline_state import adopt_library, writer_lock

            with writer_lock(args.state.parent):
                result = adopt_library(args.source, args.manifest, args.state)
            print(f"Library adoption complete: {result['adopted']} tracks recorded in {args.state}")
            return 0
        if args.command == "status":
            from pipeline_state import state_summary

            summary = state_summary(args.state)
            print("Pipeline state")
            for key, value in summary.items():
                print(f"  {key.replace('_', ' '):<27} {value}")
            return 0
        if args.command == "migrate-lastfm-cache":
            from pipeline_state import import_legacy_lastfm_cache, writer_lock

            with writer_lock(args.state.parent):
                result = import_legacy_lastfm_cache(args.source, args.state)
            print(f"Last.fm cache migration complete: {result['imported']} responses recorded")
            return 0
        errors = verify_manifest(args.root, args.manifest)
        if errors:
            print(f"Verification FAILED: {len(errors)} error(s)")
            for error in errors:
                print(f"  {error}")
            return 1
        print(
            "Verification passed: every manifest file exists, matches its checksum, and is readable."
        )
        return 0
    except SafetyError as exc:
        print(f"Safety check failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
