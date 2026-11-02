"""Review-first planning and verified staging for a rebuilt music library.

Evidence collection writes only a private SQLite cache and reports. Media copy
is available solely through an explicit staging command that requires an empty
destination, verifies every checksum, and never edits a source file.
"""

from __future__ import annotations

import argparse
import configparser
import csv
import json
import re
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from library_safety import (
    DEFAULT_BACKUP,
    MANIFEST_NAME,
    SafetyError,
    atomic_verified_copy,
    load_manifest,
    verify_manifest,
)
from music_taxonomy import classify_tag_evidence
from music_paths import load_paths

STATE = load_paths().state / "rebuild-evidence.sqlite3"
REPORTS = load_paths().state / "rebuild-reports"
CONFIG = Path.home() / ".config" / "genre-tagger.cfg"
SORTED_LIBRARY = Path.home() / "Music" / "Sorted-Library"
SORTED_MANIFEST = ".sorted-library-manifest.jsonl"


def _connect(state: Path) -> sqlite3.Connection:
    state.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state)
    db.execute("""CREATE TABLE IF NOT EXISTS lastfm_evidence (
        identity TEXT NOT NULL, endpoint TEXT NOT NULL, status TEXT NOT NULL,
        payload TEXT, error TEXT, fetched_at TEXT NOT NULL,
        PRIMARY KEY(identity, endpoint))""")
    return db


def _api_key(config: Path) -> str:
    parser = configparser.ConfigParser()
    if not config.is_file():
        raise SafetyError(f"Last.fm config does not exist: {config}")
    parser.read(config)
    key = parser.get("lastfm", "api_key", fallback="").strip()
    if not key:
        raise SafetyError(f"No [lastfm] api_key in {config}")
    return key


def _identity(record: dict) -> tuple[str, str, str] | None:
    artist, title = record.get("artist", "").strip(), record.get("title", "").strip()
    if not artist or not title or artist.casefold() in {"unknown artist", "various artists"}:
        return None
    return artist, title, record.get("album", "").strip()


def _fetch(key: str, method: str, params: dict[str, str]) -> dict:
    query = {
        "method": method,
        "api_key": key,
        "format": "json",
        "autocorrect": "0",
        **params,
    }
    request = urllib.request.Request(
        "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(query),
        headers={"User-Agent": "download.py library rebuild/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
            if "error" in payload:
                return {
                    "status": "api_error",
                    "error": f"Last.fm {payload.get('error')}: {payload.get('message', 'unknown error')}",
                    "payload": payload,
                }
            return {
                "status": "ok",
                "payload": payload,
            }
    except urllib.error.HTTPError as exc:
        return {"status": "http_error", "error": f"HTTP {exc.code}"}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"status": "network_error", "error": f"{type(exc).__name__}: {exc}"}


def collect_evidence(
    records: list[dict], state: Path, config: Path, delay: float, refresh: bool = False
) -> dict[str, int]:
    key, db = _api_key(config), _connect(state)
    counts: dict[str, int] = defaultdict(int)
    try:
        for record in records:
            identity = _identity(record)
            if not identity:
                counts["missing_identity"] += 1
                continue
            artist, title, album = identity
            token = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
            endpoints = [
                ("track", "track.getTopTags", {"artist": artist, "track": title}),
                ("artist", "artist.getTopTags", {"artist": artist}),
            ]
            if album and album.casefold() != "unknown album":
                endpoints.append(("album", "album.getTopTags", {"artist": artist, "album": album}))
            for endpoint, method, params in endpoints:
                exists = db.execute(
                    "SELECT status, payload FROM lastfm_evidence WHERE identity=? AND endpoint=?",
                    (token, endpoint),
                ).fetchone()
                # Last.fm can report errors in a HTTP-200 JSON response. Those
                # transient failures must be retried, rather than cached forever.
                cached_api_error = bool(exists and exists[1] and '"error"' in exists[1])
                if exists and not refresh and not cached_api_error:
                    counts["cached"] += 1
                    continue
                result = _fetch(key, method, params)
                db.execute(
                    "INSERT OR REPLACE INTO lastfm_evidence VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        token,
                        endpoint,
                        result["status"],
                        json.dumps(result.get("payload"), ensure_ascii=False),
                        result.get("error"),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                db.commit()
                counts[result["status"]] += 1
                time.sleep(delay)
    finally:
        db.close()
    return dict(counts)


def _safe_component(value: str, fallback: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    value = re.sub(r"[/:\\\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:150]


def _classify(record: dict, db: sqlite3.Connection) -> tuple[str, float, str]:
    identity = _identity(record)
    if not identity:
        return "Review", 0.0, "missing trusted artist/title identity"
    token = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    rows = db.execute(
        "SELECT endpoint, status, payload, error FROM lastfm_evidence WHERE identity=?",
        (token,),
    ).fetchall()
    if not rows:
        return "Review", 0.0, "no cached Last.fm evidence"
    evidence = []
    for endpoint, status, payload, error in rows:
        tags = []
        try:
            if status == "ok" and payload:
                tags = json.loads(payload).get("toptags", {}).get("tag", [])
        except (json.JSONDecodeError, AttributeError):
            status = "invalid_payload"
        if isinstance(tags, dict):
            tags = [tags]
        evidence.append((endpoint, status, tags))
    classification = classify_tag_evidence(evidence)
    return classification.genre, classification.confidence, classification.reason


def create_plan(
    records: list[dict], state: Path, plan_path: Path, review_path: Path
) -> dict[str, int]:
    db = _connect(state)
    try:
        classifications = [_classify(record, db) for record in records]
    finally:
        db.close()
    album_counts: dict[tuple[str, str], int] = defaultdict(int)
    for record in records:
        artist, album = (
            record.get("artist", "").strip(),
            record.get("album", "").strip(),
        )
        if artist and album and album.casefold() != "unknown album":
            album_counts[(artist.casefold(), album.casefold())] += 1
    rows, destinations = [], defaultdict(list)
    for record, (genre, confidence, reason) in zip(records, classifications):
        artist = _safe_component(record.get("artist", ""), "Unknown Artist")
        album = _safe_component(record.get("album", ""), "Unknown Album")
        source_name = _safe_component(Path(record["relative_path"]).stem, "Untitled") + ".m4a"
        key = (
            record.get("artist", "").strip().casefold(),
            record.get("album", "").strip().casefold(),
        )
        components = [genre, artist]
        if album_counts[key] >= 5 and record.get("album", "").strip().casefold() != "unknown album":
            components.append(album)
        destination = "/".join(components + [source_name])
        row = {
            "source": record["relative_path"],
            "sha256": record["sha256"],
            "artist": record.get("artist", ""),
            "title": record.get("title", ""),
            "album": record.get("album", ""),
            "genre": genre,
            "confidence": round(confidence, 3),
            "reason": reason,
            "destination": destination,
            "status": "ready" if genre != "Review" else "review",
        }
        rows.append(row)
        destinations[destination.casefold()].append(row)
    for same in destinations.values():
        if len(same) > 1:
            for row in same:
                row["status"] = "review"
                row["genre"] = "Review"
                row["reason"] = "destination collision: " + row["destination"]
                collision_name = _safe_component(Path(row["source"]).stem, "track")
                row["destination"] = f"Review/Collision/{collision_name}--{row['sha256'][:12]}.m4a"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    with plan_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    review_rows = [row for row in rows if row["status"] != "ready"]
    with review_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "source",
                "artist",
                "title",
                "album",
                "genre",
                "confidence",
                "reason",
                "destination",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(review_rows)
    return {
        "tracks": len(rows),
        "ready": len(rows) - len(review_rows),
        "review": len(review_rows),
        "collisions": sum(
            1 for r in review_rows if r["reason"].startswith("destination collision")
        ),
    }


def load_plan(path: Path) -> list[dict]:
    if not path.is_file():
        raise SafetyError(f"Rebuild plan does not exist: {path}")
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except json.JSONDecodeError as exc:
        raise SafetyError(f"Rebuild plan is not valid JSONL: {path}: {exc}") from exc
    if not rows:
        raise SafetyError("Refusing to stage an empty rebuild plan.")
    return rows


def stage_plan(plan_path: Path, source_root: Path, destination: Path) -> dict[str, int]:
    """Copy a complete plan with per-file checksum verification.

    The source is never edited. The target must be empty and no destination is
    overwritten; a partial target is deliberately retained for inspection.
    """
    source_root = source_root.resolve()
    destination = destination.resolve()
    if not source_root.is_dir():
        raise SafetyError(f"Verified source root does not exist: {source_root}")
    if destination == source_root or destination.is_relative_to(source_root):
        raise SafetyError("Staged destination cannot be inside the verified source root.")
    if destination.exists() and not destination.is_dir():
        raise SafetyError(f"Staged destination is not a directory: {destination}")
    if destination.exists() and any(destination.iterdir()):
        raise SafetyError(f"Staged destination is not empty (refusing to overwrite): {destination}")

    plan = load_plan(plan_path)
    source_manifest = {
        row["relative_path"]: row for row in load_manifest(source_root / MANIFEST_NAME)
    }
    planned_sources = [row.get("source") for row in plan]
    if len(planned_sources) != len(set(planned_sources)):
        raise SafetyError("Plan contains duplicate source entries.")
    if set(planned_sources) != set(source_manifest):
        missing = len(set(source_manifest) - set(planned_sources))
        extra = len(set(planned_sources) - set(source_manifest))
        raise SafetyError(f"Plan/source manifest mismatch: missing={missing}, extra={extra}")
    targets: set[str] = set()
    for row in plan:
        source = row["source"]
        target = Path(row.get("destination", ""))
        if not source or target.is_absolute() or ".." in target.parts or not target.parts:
            raise SafetyError(f"Unsafe source or destination in plan: {source!r} -> {target!s}")
        key = target.as_posix().casefold()
        if key in targets:
            raise SafetyError(f"Plan still has a destination collision: {target}")
        targets.add(key)
        if row.get("sha256") != source_manifest[source].get("sha256"):
            raise SafetyError(f"Plan checksum does not match source manifest: {source}")

    destination.mkdir(parents=True, exist_ok=True)
    staged_manifest: list[dict] = []
    for number, row in enumerate(plan, 1):
        atomic_verified_copy(
            source_root / row["source"],
            destination / row["destination"],
            row["sha256"],
        )
        staged_manifest.append(
            {
                "relative_path": Path(row["destination"]).as_posix(),
                "size": source_manifest[row["source"]]["size"],
                "sha256": row["sha256"],
                "source_relative_path": row["source"],
                "genre": row["genre"],
                "status": row["status"],
            }
        )
        if number % 50 == 0 or number == len(plan):
            print(f"  [{number:>4}/{len(plan)}] checksum-verified copies staged")
    manifest_path = destination / SORTED_MANIFEST
    _write_manifest(manifest_path, staged_manifest)
    reconcile_staged_plan(plan_path, source_root, destination)
    return {
        "tracks": len(plan),
        "ready": sum(row["status"] == "ready" for row in plan),
        "review": sum(row["status"] != "ready" for row in plan),
    }


def _write_manifest(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def reconcile_staged_plan(plan_path: Path, source_root: Path, destination: Path) -> dict[str, int]:
    """Verify a staged plan and canonicalize its manifest to real path casing.

    APFS is normally case-insensitive. Two source artist spellings that differ
    only by case therefore share one real directory. This function accepts
    only one-to-one casefolded paths; every other discrepancy is an error.
    """
    plan = load_plan(plan_path)
    destination = destination.resolve()
    source_root = source_root.resolve()
    source_manifest = {
        row["relative_path"]: row for row in load_manifest(source_root / MANIFEST_NAME)
    }
    actual = {path.relative_to(destination).as_posix(): path for path in destination.rglob("*.m4a")}
    folded: dict[str, list[str]] = defaultdict(list)
    for relative in actual:
        folded[relative.casefold()].append(relative)
    canonical_rows: list[dict] = []
    used: set[str] = set()
    for row in plan:
        expected = row["destination"]
        matches = folded.get(expected.casefold(), [])
        if len(matches) != 1:
            raise SafetyError(
                f"Staged path is missing or non-unique for {expected!r}: matches={len(matches)}"
            )
        real_path = matches[0]
        if real_path in used:
            raise SafetyError(f"Multiple plan entries resolve to staged file: {real_path}")
        used.add(real_path)
        source = row["source"]
        canonical_rows.append(
            {
                "relative_path": real_path,
                "size": source_manifest[source]["size"],
                "sha256": row["sha256"],
                "source_relative_path": source,
                "genre": row["genre"],
                "status": row["status"],
            }
        )
    if set(actual) != used:
        raise SafetyError(
            f"Staged library has unexpected or unplanned media: {len(set(actual) - used)} file(s)"
        )
    manifest_path = destination / SORTED_MANIFEST
    _write_manifest(manifest_path, canonical_rows)
    errors = verify_manifest(destination, manifest_path)
    if errors:
        raise SafetyError(
            f"Staged-library reconciliation failed with {len(errors)} error(s): {errors[0]}"
        )
    return {
        "tracks": len(canonical_rows),
        "casefolded_paths": sum(
            row["destination"] != canonical["relative_path"]
            for row, canonical in zip(plan, canonical_rows)
        ),
    }


def write_genre_playlists(plan_path: Path, destination: Path) -> dict[str, int]:
    """Create importable M3U8 genre playlists for verified, non-review tracks."""
    plan = load_plan(plan_path)
    destination = destination.resolve()
    errors = verify_manifest(destination, destination / SORTED_MANIFEST)
    if errors:
        raise SafetyError(
            "Refusing playlist generation: staged library is not reconciled: " + errors[0]
        )
    playlists = destination / "Playlists"
    if playlists.exists() and any(playlists.iterdir()):
        raise SafetyError(f"Playlist directory is not empty (refusing to overwrite): {playlists}")
    playlists.mkdir(parents=True, exist_ok=True)
    actual_by_source = {
        row["source_relative_path"]: destination / row["relative_path"]
        for row in load_manifest(destination / SORTED_MANIFEST)
    }
    groups: dict[str, list[Path]] = defaultdict(list)
    for row in plan:
        if row["status"] == "ready":
            groups[row["genre"]].append(actual_by_source[row["source"]])
    for genre, tracks in sorted(groups.items()):
        path = playlists / f"{_safe_component(genre, 'Genre')}.m3u8"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("#EXTM3U\n")
            for track in tracks:
                handle.write(str(track) + "\n")
    return {"playlists": len(groups), "tracks": sum(map(len, groups.values()))}


def relocate_staged_library(source: Path, destination: Path) -> None:
    """Atomically rename a verified staged library and repair M3U8 paths."""
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise SafetyError(f"Staged library does not exist: {source}")
    if destination.exists():
        raise SafetyError(f"New library path already exists (refusing to overwrite): {destination}")
    errors = verify_manifest(source, source / SORTED_MANIFEST)
    if errors:
        raise SafetyError(f"Refusing to rename an unreconciled staged library: {errors[0]}")
    playlist_dir = source / "Playlists"
    replacements: list[tuple[Path, str]] = []
    if playlist_dir.exists():
        for playlist in sorted(playlist_dir.glob("*.m3u8")):
            text = playlist.read_text(encoding="utf-8")
            replacements.append((playlist, text.replace(str(source), str(destination))))
    # Publish each tiny playlist replacement before the directory rename. Their
    # old path is still readable until the atomic rename is published.
    for playlist, text in replacements:
        temporary = playlist.with_name(f".{playlist.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(text, encoding="utf-8", newline="\n")
            temporary.replace(playlist)
        finally:
            temporary.unlink(missing_ok=True)
    source.rename(destination)
    errors = verify_manifest(destination, destination / SORTED_MANIFEST)
    if errors:
        raise SafetyError(f"Renamed library verification failed: {errors[0]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Non-destructive Last.fm evidence cache and rebuild planner."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    evidence = sub.add_parser("evidence", help="Cache Last.fm evidence only; never touches media.")
    evidence.add_argument("--manifest", type=Path, default=DEFAULT_BACKUP / MANIFEST_NAME)
    evidence.add_argument("--state", type=Path, default=STATE)
    evidence.add_argument("--config", type=Path, default=CONFIG)
    evidence.add_argument("--delay", type=float, default=0.25)
    evidence.add_argument("--refresh", action="store_true")
    plan = sub.add_parser(
        "plan", help="Write a dry-run copy plan and review CSV; never copies media."
    )
    plan.add_argument("--manifest", type=Path, default=DEFAULT_BACKUP / MANIFEST_NAME)
    plan.add_argument("--state", type=Path, default=STATE)
    plan.add_argument("--reports", type=Path, default=REPORTS)
    stage = sub.add_parser(
        "stage",
        help="Checksum-verified copy to an empty staged library; never edits sources.",
    )
    stage.add_argument("--plan", type=Path, required=True)
    stage.add_argument("--source", type=Path, default=DEFAULT_BACKUP)
    stage.add_argument("--destination", type=Path, default=SORTED_LIBRARY)
    stage.add_argument(
        "--confirm-stage-copy",
        action="store_true",
        help="Required acknowledgement before creating staged media copies.",
    )
    reconcile = sub.add_parser(
        "reconcile",
        help="Read-only media verification and manifest reconciliation for a staged plan.",
    )
    reconcile.add_argument("--plan", type=Path, required=True)
    reconcile.add_argument("--source", type=Path, default=DEFAULT_BACKUP)
    reconcile.add_argument("--destination", type=Path, default=SORTED_LIBRARY)
    playlists = sub.add_parser(
        "playlists", help="Write genre M3U8 files for the verified staged library."
    )
    playlists.add_argument("--plan", type=Path, required=True)
    playlists.add_argument("--destination", type=Path, default=SORTED_LIBRARY)
    relocate = sub.add_parser(
        "relocate", help="Rename a verified staged library without changing media."
    )
    relocate.add_argument("--source", type=Path, default=SORTED_LIBRARY)
    relocate.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "evidence":
            records = load_manifest(args.manifest)
            counts = collect_evidence(records, args.state, args.config, args.delay, args.refresh)
            print(
                "Last.fm evidence cache updated: "
                + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            )
        elif args.command == "plan":
            records = load_manifest(args.manifest)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            report_dir = args.reports / stamp
            report = create_plan(
                records,
                args.state,
                report_dir / "copy-plan.jsonl",
                report_dir / "review.csv",
            )
            print(
                "Dry-run plan written (no media copied): "
                + ", ".join(f"{k}={v}" for k, v in report.items())
            )
            print(
                f"Plan: {report_dir / 'copy-plan.jsonl'}\nReview queue: {report_dir / 'review.csv'}"
            )
        elif args.command == "stage":
            if not args.confirm_stage_copy:
                raise SafetyError(
                    "Refusing to copy media without --confirm-stage-copy. "
                    "This copy is staged and source-preserving, but creates a new library."
                )
            report = stage_plan(args.plan, args.source, args.destination)
            print(
                "Staged-library reconciliation passed: "
                + ", ".join(f"{key}={value}" for key, value in report.items())
            )
            print(f"Manifest: {args.destination / SORTED_MANIFEST}")
        elif args.command == "reconcile":
            report = reconcile_staged_plan(args.plan, args.source, args.destination)
            print(
                "Staged-library reconciliation passed: "
                + ", ".join(f"{key}={value}" for key, value in report.items())
            )
            print(f"Manifest: {args.destination / SORTED_MANIFEST}")
        elif args.command == "playlists":
            report = write_genre_playlists(args.plan, args.destination)
            print(
                "Genre playlists written: "
                + ", ".join(f"{key}={value}" for key, value in report.items())
            )
            print(f"Playlist directory: {args.destination / 'Playlists'}")
        else:
            relocate_staged_library(args.source, args.destination)
            print(f"Verified staged library renamed: {args.source} -> {args.destination}")
        return 0
    except SafetyError as exc:
        print(f"Safety check failed: {exc}")
        return 2
