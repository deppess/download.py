"""Review-first enrichment, immutable planning, and verified incremental publication."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from library_safety import SafetyError, atomic_verified_copy, inspect_file, sha256
from music_paths import load_paths
from music_taxonomy import TAXONOMY_VERSION, classify_tag_evidence
from pipeline_state import connect, utc_now, writer_lock
from provider_clients import LastFMClient, LRCLIBClient, ProviderConfigurationError


def _safe_component(value: str, fallback: str) -> str:
    import re
    import unicodedata

    value = unicodedata.normalize("NFC", value).strip()
    value = re.sub(r"[/:\\\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value or fallback)[:150]


def _raw_path(landing: Path, row) -> Path:
    return landing / row["relative_path"]


def _tags(payload: dict | None) -> list[dict]:
    values = (payload or {}).get("toptags", {}).get("tag", [])
    return [values] if isinstance(values, dict) else values if isinstance(values, list) else []


def _source_artist(raw: Path) -> tuple[str, str]:
    """Return declared source artist and uploader separately; never conflate them."""
    sidecar = raw.with_suffix(".info.json")
    if not sidecar.is_file():
        return "", ""
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return str(payload.get("artist") or "").strip(), str(
        payload.get("uploader") or payload.get("channel") or ""
    ).strip()


def enrich(
    state_file: Path, landing: Path, refresh: bool = False, with_lyrics: bool = False
) -> dict[str, int]:
    """Read raw media metadata and cache evidence; no media is modified."""
    db = connect(state_file)
    counts: Counter[str] = Counter()
    try:
        rows = db.execute("SELECT * FROM media_files WHERE role='raw' ORDER BY id").fetchall()
        try:
            lastfm = LastFMClient(db)
        except ProviderConfigurationError:
            lastfm = None
        lyrics = LRCLIBClient(db) if with_lyrics else None
        for row in rows:
            raw = _raw_path(landing, row)
            if not raw.is_file() or sha256(raw) != row["sha256"]:
                db.execute(
                    "UPDATE media_files SET review_status='review', classification_reason=? WHERE id=?",
                    ("raw source missing or checksum changed", row["id"]),
                )
                counts["invalid_raw"] += 1
                continue
            metadata = inspect_file(raw, landing, include_hash=False)
            artist, title, album = metadata["artist"], metadata["title"], metadata["album"]
            declared_artist, uploader = _source_artist(raw)
            uploader_only = bool(
                uploader
                and artist
                and artist.casefold() == uploader.casefold()
                and (not declared_artist or declared_artist.casefold() == uploader.casefold())
            )
            if (
                not metadata["readable"]
                or not artist
                or not title
                or uploader_only
                or artist.casefold() in {"unknown artist", "various artists"}
            ):
                db.execute(
                    """UPDATE media_files SET artist=?, title=?, album=?, duration=?, review_status='review',
                       classification_reason=?, taxonomy_version=?, enriched_at=? WHERE id=?""",
                    (
                        artist,
                        title,
                        album,
                        metadata["duration"],
                        "uploader/channel is not accepted as artist identity"
                        if uploader_only
                        else "missing trusted artist/title metadata",
                        TAXONOMY_VERSION,
                        utc_now(),
                        row["id"],
                    ),
                )
                counts["review"] += 1
                continue
            evidence = []
            if lastfm is None:
                evidence.append(("track", "configuration_missing", []))
            else:
                for endpoint in ("track", "artist") + (
                    ("album",) if album and album.casefold() != "unknown album" else ()
                ):
                    result = lastfm.top_tags(endpoint, artist, title, album, refresh)
                    evidence.append((endpoint, result.status, _tags(result.payload)))
                    counts[f"lastfm_{result.status}"] += 1
            if lyrics:
                result = lyrics.lyrics(
                    artist, title, album, round(metadata["duration"] or 0), refresh
                )
                counts[f"lrclib_{result.status}"] += 1
            classification = classify_tag_evidence(evidence)
            db.execute(
                """UPDATE media_files SET artist=?, title=?, album=?, duration=?, genre=?, review_status=?,
                   classification_reason=?, taxonomy_version=?, enriched_at=?, verified_at=? WHERE id=?""",
                (
                    artist,
                    title,
                    album,
                    metadata["duration"],
                    classification.genre,
                    classification.status,
                    classification.reason,
                    TAXONOMY_VERSION,
                    utc_now(),
                    utc_now(),
                    row["id"],
                ),
            )
            counts[classification.status] += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return dict(counts)


def write_review(state_file: Path, destination: Path) -> int:
    db = connect(state_file)
    try:
        rows = db.execute("""SELECT id, relative_path, sha256, artist, title, album, genre,
                           classification_reason FROM media_files
                           WHERE role='raw' AND COALESCE(review_status, 'review') != 'ready'
                           ORDER BY id""").fetchall()
    finally:
        db.close()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "id",
                "relative_path",
                "sha256",
                "artist",
                "title",
                "album",
                "genre",
                "classification_reason",
            ],
        )
        writer.writeheader()
        writer.writerows(map(dict, rows))
    temporary.replace(destination)
    return len(rows)


def approve_review_item(state_file: Path, media_id: int, genre: str) -> None:
    if genre not in GENRE_PLAYLISTS:
        raise SafetyError(f"Review genre must be one of: {', '.join(GENRE_PLAYLISTS)}")
    db = connect(state_file)
    try:
        row = db.execute(
            "SELECT role, artist, title FROM media_files WHERE id=?", (media_id,)
        ).fetchone()
        if not row or row["role"] != "raw":
            raise SafetyError(f"Raw review item does not exist: {media_id}")
        if not row["artist"] or not row["title"]:
            raise SafetyError("Cannot approve an item without artist and title metadata")
        db.execute(
            """UPDATE media_files SET genre=?, review_status='ready', classification_reason=?,
               taxonomy_version=?, enriched_at=? WHERE id=?""",
            (genre, "manual review approval", TAXONOMY_VERSION, utc_now(), media_id),
        )
        db.commit()
    finally:
        db.close()


def _plan_digest(document: dict) -> str:
    copy = dict(document)
    copy.pop("sha256", None)
    return hashlib.sha256(
        json.dumps(copy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def load_plan(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyError(f"Invalid plan file {path}: {exc}") from exc
    if (
        document.get("version") != 1
        or not document.get("plan_id")
        or not isinstance(document.get("items"), list)
    ):
        raise SafetyError("Plan is missing required immutable-plan fields")
    if document.get("sha256") != _plan_digest(document):
        raise SafetyError("Plan checksum does not match its contents")
    return document


def create_plan(state_file: Path, library: Path, destination: Path) -> dict[str, int | str]:
    library = library.resolve()
    if destination.exists():
        raise SafetyError(
            f"Plan destination already exists (refusing to replace it): {destination}"
        )
    db = connect(state_file)
    try:
        candidates = db.execute(
            "SELECT * FROM media_files WHERE role='raw' AND review_status='ready' ORDER BY id"
        ).fetchall()
        existing_hashes = {
            row[0] for row in db.execute("SELECT sha256 FROM media_files WHERE role='library'")
        }
        candidates = [row for row in candidates if row["sha256"] not in existing_hashes]
        if not candidates:
            raise SafetyError("No review-approved raw tracks are available for a new plan")
        album_counts = Counter(
            (row["genre"], row["artist"].casefold(), row["album"].casefold())
            for row in candidates
            if row["album"] and row["album"].casefold() != "unknown album"
        )
        used = (
            {path.relative_to(library).as_posix().casefold() for path in library.rglob("*.m4a")}
            if library.is_dir()
            else set()
        )
        items = []
        for row in candidates:
            components = [
                _safe_component(row["genre"], "Review"),
                _safe_component(row["artist"], "Unknown Artist"),
            ]
            album_key = (row["genre"], row["artist"].casefold(), row["album"].casefold())
            if album_counts[album_key] >= 5:
                components.append(_safe_component(row["album"], "Unknown Album"))
            filename = f"{_safe_component(row['title'], 'Untitled')} [{row['sha256'][:12]}].m4a"
            relative = Path(*components, filename).as_posix()
            if relative.casefold() in used:
                raise SafetyError(f"Destination collision with existing library path: {relative}")
            used.add(relative.casefold())
            items.append(
                {
                    "raw_media_id": row["id"],
                    "raw_sha256": row["sha256"],
                    "destination": relative,
                    "genre": row["genre"],
                }
            )
        plan_id = uuid.uuid4().hex
        document = {
            "version": 1,
            "plan_id": plan_id,
            "created_at": utc_now(),
            "library": str(library),
            "items": items,
        }
        document["sha256"] = _plan_digest(document)
        _write_json(destination, document)
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO plan_runs(plan_id, created_at, status, summary_json) VALUES (?, ?, 'pending', ?)",
            (
                plan_id,
                utc_now(),
                json.dumps(
                    {
                        "plan_sha256": document["sha256"],
                        "path": str(destination),
                        "items": len(items),
                    }
                ),
            ),
        )
        for item in items:
            db.execute(
                """INSERT INTO plan_items(plan_id, raw_media_id, raw_sha256, destination_relative_path, genre, status, created_at)
                          VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    plan_id,
                    item["raw_media_id"],
                    item["raw_sha256"],
                    item["destination"],
                    item["genre"],
                    utc_now(),
                ),
            )
        db.commit()
        return {"plan_id": plan_id, "items": len(items), "path": str(destination)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def approve_plan(state_file: Path, path: Path) -> int:
    plan = load_plan(path)
    db = connect(state_file)
    try:
        run = db.execute(
            "SELECT summary_json, status FROM plan_runs WHERE plan_id=?", (plan["plan_id"],)
        ).fetchone()
        if (
            not run
            or json.loads(run["summary_json"])["plan_sha256"] != plan["sha256"]
            or run["status"] != "pending"
        ):
            raise SafetyError("Plan is unknown, changed, or no longer pending")
        result = db.execute(
            "UPDATE plan_items SET status='approved', approved_at=? WHERE plan_id=? AND status='pending'",
            (utc_now(), plan["plan_id"]),
        )
        db.execute("UPDATE plan_runs SET status='approved' WHERE plan_id=?", (plan["plan_id"],))
        db.commit()
        return result.rowcount
    finally:
        db.close()


def publish_plan(state_file: Path, landing: Path, library: Path, path: Path) -> int:
    plan = load_plan(path)
    library = library.resolve()
    db = connect(state_file)
    published = 0
    try:
        run = db.execute(
            "SELECT status, summary_json FROM plan_runs WHERE plan_id=?", (plan["plan_id"],)
        ).fetchone()
        if (
            not run
            or run["status"] != "approved"
            or json.loads(run["summary_json"])["plan_sha256"] != plan["sha256"]
        ):
            raise SafetyError("Plan is not approved or no longer matches state")
        items = db.execute(
            """SELECT p.*, m.relative_path FROM plan_items p JOIN media_files m ON m.id=p.raw_media_id
                              WHERE p.plan_id=? AND p.status='approved' ORDER BY p.id""",
            (plan["plan_id"],),
        ).fetchall()
        if not items:
            raise SafetyError("Plan has no approved items to publish")
        for item in items:
            source = landing / item["relative_path"]
            target = library / item["destination_relative_path"]
            if not source.is_file() or sha256(source) != item["raw_sha256"]:
                raise SafetyError(f"Stale or missing raw source: {source}")
            if target.exists():
                raise SafetyError(f"Destination now exists: {target}")
            if db.execute(
                "SELECT 1 FROM media_files WHERE role='library' AND sha256=?", (item["raw_sha256"],)
            ).fetchone():
                raise SafetyError(f"Duplicate library checksum detected: {item['raw_sha256']}")
        for item in items:
            source, target = (
                landing / item["relative_path"],
                library / item["destination_relative_path"],
            )
            atomic_verified_copy(source, target, item["raw_sha256"])
            inspected = inspect_file(target, library, include_hash=False)
            if not inspected["readable"]:
                raise SafetyError(f"Published file failed readability verification: {target}")
            db.execute(
                """INSERT INTO media_files(role, relative_path, source_relative_path, sha256, size, genre, review_status, readable, verified_at, artist, title, album, duration)
                          VALUES ('library', ?, ?, ?, ?, ?, 'ready', 1, ?, ?, ?, ?, ?)""",
                (
                    item["destination_relative_path"],
                    item["relative_path"],
                    item["raw_sha256"],
                    target.stat().st_size,
                    item["genre"],
                    utc_now(),
                    inspected["artist"],
                    inspected["title"],
                    inspected["album"],
                    inspected["duration"],
                ),
            )
            db.execute(
                "UPDATE plan_items SET status='published', published_at=? WHERE id=?",
                (utc_now(), item["id"]),
            )
            published += 1
        db.execute("UPDATE plan_runs SET status='published' WHERE plan_id=?", (plan["plan_id"],))
        db.commit()
        return published
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


GENRE_PLAYLISTS = ("Electronic", "Hip Hop", "Pop", "Rock & Metal", "Soul")


def write_genre_playlists(state_file: Path, library: Path) -> dict[str, int]:
    """Atomically refresh only the five allowed genre playlists."""
    library = library.resolve()
    db = connect(state_file)
    try:
        rows = db.execute(
            """SELECT relative_path, sha256, genre FROM media_files
               WHERE role='library' AND review_status='ready' AND genre IN (?, ?, ?, ?, ?)
               ORDER BY genre, relative_path""",
            GENRE_PLAYLISTS,
        ).fetchall()
    finally:
        db.close()
    groups: dict[str, list[Path]] = defaultdict(list)
    for row in rows:
        track = library / row["relative_path"]
        if not track.is_file() or sha256(track) != row["sha256"]:
            raise SafetyError(f"Refusing playlist update: library state no longer verifies {track}")
        groups[row["genre"]].append(track)
    playlist_dir = library / "Playlists"
    playlist_dir.mkdir(parents=True, exist_ok=True)
    for genre in GENRE_PLAYLISTS:
        output = playlist_dir / f"{genre}.m3u8"
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("#EXTM3U\n")
            for track in groups[genre]:
                handle.write(str(track) + "\n")
        temporary.replace(output)
    return {"playlists": len(GENRE_PLAYLISTS), "tracks": sum(map(len, groups.values()))}


def create_import_bundle(
    state_file: Path, library: Path, plan_path: Path, destination: Path
) -> int:
    """Write a verified M3U8 handoff bundle; never invokes Apple Music."""
    plan = load_plan(plan_path)
    db = connect(state_file)
    try:
        items = db.execute(
            """SELECT destination_relative_path, raw_sha256 FROM plan_items
               WHERE plan_id=? AND status='published' ORDER BY id""",
            (plan["plan_id"],),
        ).fetchall()
    finally:
        db.close()
    if not items:
        raise SafetyError("Import bundle requires at least one published plan item")
    library = library.resolve()
    verified = []
    for item in items:
        track = library / item["destination_relative_path"]
        if not track.is_file() or sha256(track) != item["raw_sha256"]:
            raise SafetyError(f"Import bundle verification failed: {track}")
        verified.append(track)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("#EXTM3U\n")
        for track in verified:
            handle.write(str(track) + "\n")
    temporary.replace(destination)
    return len(verified)


def main(argv: list[str] | None = None) -> int:
    paths = load_paths()
    parser = argparse.ArgumentParser(
        description="Review-first enrichment, planning, and verified publishing."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    enrich_parser = sub.add_parser(
        "enrich", help="Cache evidence and classify raw media; never edits audio."
    )
    enrich_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    enrich_parser.add_argument("--landing", type=Path, default=paths.landing)
    enrich_parser.add_argument("--refresh", action="store_true")
    enrich_parser.add_argument(
        "--with-lyrics", action="store_true", help="Also query/cache LRCLIB evidence."
    )
    review_parser = sub.add_parser(
        "review", help="Write a CSV of raw items requiring human review."
    )
    review_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    review_parser.add_argument(
        "--output", type=Path, default=paths.state / "reports" / "raw-review.csv"
    )
    review_parser.add_argument("action", nargs="?", choices=("approve",))
    review_parser.add_argument("--id", type=int, help="Raw media ID to approve after review.")
    review_parser.add_argument(
        "--genre", choices=GENRE_PLAYLISTS, help="Approved destination genre."
    )
    review_parser.add_argument(
        "--confirm-review",
        action="store_true",
        help="Required acknowledgement for a manual review decision.",
    )
    plan_parser = sub.add_parser(
        "plan", help="Create, approve, or publish immutable incremental plans."
    )
    plan_sub = plan_parser.add_subparsers(dest="plan_command", required=True)
    create = plan_sub.add_parser("create", help="Create a pending plan from ready raw items.")
    create.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    create.add_argument("--library", type=Path, default=paths.library)
    create.add_argument("--output", type=Path, default=paths.state / "plans" / "incoming-plan.json")
    approve = plan_sub.add_parser(
        "approve", help="Mark every immutable plan item approved after review."
    )
    approve.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    approve.add_argument("--plan", type=Path, required=True)
    approve.add_argument(
        "--confirm-approve",
        action="store_true",
        help="Required acknowledgement after reviewing the plan.",
    )
    publish = plan_sub.add_parser(
        "publish", help="Checksum-verified copies into the existing library."
    )
    publish.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    publish.add_argument("--landing", type=Path, default=paths.landing)
    publish.add_argument("--library", type=Path, default=paths.library)
    publish.add_argument("--plan", type=Path, required=True)
    publish.add_argument(
        "--confirm-publish",
        action="store_true",
        help="Required acknowledgement before new library copies are made.",
    )
    playlist_parser = sub.add_parser(
        "playlists", help="Atomically update the five allowed genre playlists."
    )
    playlist_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    playlist_parser.add_argument("--library", type=Path, default=paths.library)
    bundle_parser = sub.add_parser(
        "import-bundle", help="Create a verified M3U8 handoff; never opens Apple Music."
    )
    bundle_parser.add_argument("--state", type=Path, default=paths.state / "pipeline.sqlite3")
    bundle_parser.add_argument("--library", type=Path, default=paths.library)
    bundle_parser.add_argument("--plan", type=Path, required=True)
    bundle_parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        with writer_lock(args.state.parent):
            if args.command == "enrich":
                result = enrich(args.state, args.landing, args.refresh, args.with_lyrics)
                print(
                    "Enrichment complete: "
                    + ", ".join(f"{key}={value}" for key, value in sorted(result.items()))
                )
            elif args.command == "review":
                if args.action == "approve":
                    if not args.confirm_review or args.id is None or not args.genre:
                        raise SafetyError(
                            "Review approval requires --id, --genre, and --confirm-review"
                        )
                    approve_review_item(args.state, args.id, args.genre)
                    print(f"Review item {args.id} approved for {args.genre}")
                else:
                    count = write_review(args.state, args.output)
                    print(f"Review report written: {args.output} ({count} item(s))")
            elif args.command == "plan" and args.plan_command == "create":
                result = create_plan(args.state, args.library, args.output)
                print(
                    f"Pending plan created: {result['path']} ({result['items']} item(s), id={result['plan_id']})"
                )
            elif args.command == "plan" and args.plan_command == "approve":
                if not args.confirm_approve:
                    raise SafetyError("Refusing approval without --confirm-approve")
                print(f"Approved {approve_plan(args.state, args.plan)} plan item(s)")
            elif args.command == "playlists":
                result = write_genre_playlists(args.state, args.library)
                print(
                    f"Genre playlists updated: {result['playlists']} playlists, {result['tracks']} track entries"
                )
            elif args.command == "import-bundle":
                plan = load_plan(args.plan)
                output = args.output or (paths.state / "import-bundles" / f"{plan['plan_id']}.m3u8")
                count = create_import_bundle(args.state, args.library, args.plan, output)
                print(f"Verified import bundle created: {output} ({count} track(s))")
            else:
                if not args.confirm_publish:
                    raise SafetyError("Refusing publication without --confirm-publish")
                print(
                    f"Published {publish_plan(args.state, args.landing, args.library, args.plan)} checksum-verified library copy/copies"
                )
        return 0
    except SafetyError as exc:
        print(f"Safety check failed: {exc}")
        return 2
