"""Safe, resumable intake of completed yt-dlp downloads into raw staging."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import uuid
from pathlib import Path

from library_safety import SafetyError, atomic_verified_copy, inspect_file
from music_paths import load_paths
from pipeline_state import connect, upsert_source, writer_lock

_YOUTUBE_ID = re.compile(r"\[([A-Za-z0-9_-]{11})\]$")


def sidecar_for(media: Path) -> Path:
    return media.with_suffix(".info.json")


def source_details(media: Path, sidecar: Path) -> tuple[str | None, str | None]:
    """Read only the stable yt-dlp identity fields; never trust uploader as artist."""
    payload: dict = {}
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    source_id = str(payload.get("id") or "").strip() or None
    if source_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", source_id):
        source_id = None
    if source_id is None:
        match = _YOUTUBE_ID.search(media.stem)
        source_id = match.group(1) if match else None
    source_url = (
        str(payload.get("webpage_url") or payload.get("original_url") or "").strip() or None
    )
    return source_id, source_url


def atomic_copy_sidecar(source: Path, destination: Path) -> None:
    """Copy private source evidence atomically; existing evidence is never replaced."""
    if not source.is_file() or destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def intake(source: Path, raw_root: Path, state_file: Path) -> dict[str, int]:
    source, raw_root = source.resolve(), raw_root.resolve()
    if not source.is_dir():
        raise SafetyError(f"Incoming directory does not exist: {source}")
    if raw_root == source or raw_root.is_relative_to(source):
        raise SafetyError("Raw staging must not be inside the incoming directory.")
    accepted = skipped = rejected = 0
    db = connect(state_file)
    try:
        db.execute("BEGIN IMMEDIATE")
        for media in sorted(source.rglob("*.m4a")):
            record = inspect_file(media, source, include_hash=True)
            if not record["readable"]:
                rejected += 1
                continue
            digest = record["sha256"]
            destination = raw_root / digest[:2] / f"{digest}.m4a"
            existing = db.execute(
                "SELECT id FROM media_files WHERE role='raw' AND sha256=?", (digest,)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            atomic_verified_copy(media, destination, digest)
            sidecar = sidecar_for(media)
            atomic_copy_sidecar(sidecar, destination.with_suffix(".info.json"))
            source_id, source_url = source_details(media, sidecar)
            provider_id = upsert_source(db, "youtube", source_id, source_url) if source_id else None
            db.execute(
                """INSERT INTO media_files(
                    role, relative_path, source_relative_path, sha256, size,
                    readable, adopted_at, verified_at, source_id
                ) VALUES ('raw', ?, ?, ?, ?, 1, datetime('now'), datetime('now'), ?)""",
                (
                    str(destination.relative_to(raw_root.parent)),
                    record["relative_path"],
                    digest,
                    record["size"],
                    provider_id,
                ),
            )
            accepted += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"accepted": accepted, "already_registered": skipped, "rejected": rejected}


def main(argv: list[str] | None = None) -> int:
    paths = load_paths()
    parser = argparse.ArgumentParser(
        description="Verify and preserve downloaded media in raw staging."
    )
    parser.add_argument("--source", type=Path, default=paths.landing / "incoming")
    parser.add_argument("--raw-root", type=Path, default=paths.landing / "raw")
    parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    args = parser.parse_args(argv)
    try:
        with writer_lock(args.state.parent):
            result = intake(args.source, args.raw_root, args.state)
        print("Intake complete: " + ", ".join(f"{key}={value}" for key, value in result.items()))
        return 0
    except SafetyError as exc:
        print(f"Safety check failed: {exc}")
        return 2
