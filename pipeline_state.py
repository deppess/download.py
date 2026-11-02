"""SQLite state and single-writer locking for the music pipeline."""

from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from library_safety import SafetyError, load_manifest, verify_manifest
from music_paths import load_paths

SCHEMA_VERSION = 5


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def connect(state_file: Path | None = None) -> sqlite3.Connection:
    state_file = state_file or (load_paths().state / "pipeline.sqlite3")
    state_file.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state_file)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 5000")
    migrate(db)
    return db


def migrate(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
    if 1 not in applied:
        db.executescript(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY,
                command TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                summary TEXT
            );
            CREATE TABLE media_files (
                id INTEGER PRIMARY KEY,
                role TEXT NOT NULL CHECK(role IN ('raw', 'work', 'library')),
                relative_path TEXT NOT NULL UNIQUE,
                source_relative_path TEXT,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                genre TEXT,
                review_status TEXT,
                readable INTEGER NOT NULL DEFAULT 1,
                adopted_at TEXT,
                verified_at TEXT
            );
            CREATE INDEX media_files_hash_idx ON media_files(sha256);
            CREATE INDEX media_files_role_idx ON media_files(role);
            """
        )
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (1, utc_now())
        )
        db.commit()
    if 2 not in applied:
        db.executescript(
            """
            CREATE TABLE sources (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_url TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(provider, source_id)
            );
            CREATE TABLE provider_cache (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                request_key TEXT NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT,
                error TEXT,
                fetched_at TEXT NOT NULL,
                expires_at TEXT,
                retry_after_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                UNIQUE(provider, endpoint, request_key)
            );
            CREATE INDEX provider_cache_lookup_idx
                ON provider_cache(provider, endpoint, request_key);
            CREATE TABLE plan_runs (
                id INTEGER PRIMARY KEY,
                plan_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL,
                summary_json TEXT
            );
            """
        )
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (2, utc_now())
        )
        db.commit()
    if 3 not in applied:
        db.executescript(
            """
            ALTER TABLE media_files ADD COLUMN source_id INTEGER REFERENCES sources(id);
            CREATE INDEX media_files_source_idx ON media_files(source_id);
            """
        )
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (3, utc_now())
        )
        db.commit()
    if 4 not in applied:
        db.executescript(
            """
            ALTER TABLE media_files ADD COLUMN artist TEXT;
            ALTER TABLE media_files ADD COLUMN title TEXT;
            ALTER TABLE media_files ADD COLUMN album TEXT;
            ALTER TABLE media_files ADD COLUMN duration REAL;
            ALTER TABLE media_files ADD COLUMN classification_reason TEXT;
            ALTER TABLE media_files ADD COLUMN taxonomy_version INTEGER;
            ALTER TABLE media_files ADD COLUMN enriched_at TEXT;
            CREATE TABLE plan_items (
                id INTEGER PRIMARY KEY,
                plan_id TEXT NOT NULL REFERENCES plan_runs(plan_id),
                raw_media_id INTEGER NOT NULL REFERENCES media_files(id),
                raw_sha256 TEXT NOT NULL,
                destination_relative_path TEXT NOT NULL,
                genre TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'published', 'rejected')),
                created_at TEXT NOT NULL,
                approved_at TEXT,
                published_at TEXT,
                UNIQUE(plan_id, raw_media_id),
                UNIQUE(plan_id, destination_relative_path)
            );
            CREATE INDEX plan_items_status_idx ON plan_items(plan_id, status);
            """
        )
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (4, utc_now())
        )
        db.commit()
    if 5 not in applied:
        db.executescript(
            """
            CREATE TABLE music_imports (
                id INTEGER PRIMARY KEY,
                plan_item_id INTEGER NOT NULL UNIQUE REFERENCES plan_items(id),
                persistent_id TEXT,
                reported_location TEXT,
                status TEXT NOT NULL CHECK(status IN ('intent', 'verified', 'location_mismatch', 'uncertain', 'failed')),
                error TEXT,
                created_at TEXT NOT NULL,
                verified_at TEXT
            );
            CREATE INDEX music_imports_status_idx ON music_imports(status);
            """
        )
        db.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (5, utc_now())
        )
        db.commit()


def upsert_source(
    db: sqlite3.Connection, provider: str, source_id: str, source_url: str | None
) -> int:
    now = utc_now()
    db.execute(
        """INSERT INTO sources(provider, source_id, source_url, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(provider, source_id) DO UPDATE SET
            source_url=COALESCE(excluded.source_url, sources.source_url),
            last_seen_at=excluded.last_seen_at""",
        (provider, source_id, source_url, now, now),
    )
    return db.execute(
        "SELECT id FROM sources WHERE provider=? AND source_id=?", (provider, source_id)
    ).fetchone()[0]


@contextlib.contextmanager
def writer_lock(state_dir: Path | None = None):
    state_dir = state_dir or load_paths().state
    lock_dir = state_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "writer.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "another process"
            raise SafetyError(f"Another state-changing command is active: {owner}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_at={utc_now()}\n")
        handle.flush()
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def adopt_library(source: Path, manifest: Path, state_file: Path | None = None) -> dict[str, int]:
    """Validate and record a finished library without changing its media."""
    source = source.resolve()
    manifest = manifest.resolve()
    errors = verify_manifest(source, manifest)
    if errors:
        raise SafetyError(f"Refusing adoption: library verification failed: {errors[0]}")
    records = load_manifest(manifest)
    db = connect(state_file)
    try:
        db.execute("BEGIN IMMEDIATE")
        now = utc_now()
        for record in records:
            db.execute(
                """INSERT INTO media_files(
                    role, relative_path, source_relative_path, sha256, size,
                    genre, review_status, readable, adopted_at, verified_at
                ) VALUES ('library', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    source_relative_path=excluded.source_relative_path,
                    sha256=excluded.sha256, size=excluded.size,
                    genre=excluded.genre, review_status=excluded.review_status,
                    readable=1, adopted_at=excluded.adopted_at,
                    verified_at=excluded.verified_at""",
                (
                    record["relative_path"],
                    record.get("source_relative_path"),
                    record["sha256"],
                    record["size"],
                    record.get("genre"),
                    record.get("status"),
                    now,
                    now,
                ),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"adopted": len(records)}


def state_summary(state_file: Path | None = None) -> dict[str, int]:
    db = connect(state_file)
    try:
        return {
            "library_tracks": db.execute(
                "SELECT COUNT(*) FROM media_files WHERE role='library'"
            ).fetchone()[0],
            "review_tracks": db.execute(
                "SELECT COUNT(*) FROM media_files WHERE role='library' AND review_status != 'ready'"
            ).fetchone()[0],
            "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "cached_provider_responses": db.execute(
                "SELECT COUNT(*) FROM provider_cache"
            ).fetchone()[0],
            "raw_tracks": db.execute(
                "SELECT COUNT(*) FROM media_files WHERE role='raw'"
            ).fetchone()[0],
            "pending_plan_items": db.execute(
                "SELECT COUNT(*) FROM plan_items WHERE status IN ('pending', 'approved')"
            ).fetchone()[0],
            "music_imports": db.execute(
                "SELECT COUNT(*) FROM music_imports WHERE status='verified'"
            ).fetchone()[0],
        }
    finally:
        db.close()


def import_legacy_lastfm_cache(
    legacy_state: Path, state_file: Path | None = None
) -> dict[str, int]:
    """Copy old rebuild evidence into shared cache without network access."""
    if not legacy_state.is_file():
        raise SafetyError(f"Legacy Last.fm cache does not exist: {legacy_state}")
    legacy = sqlite3.connect(legacy_state)
    try:
        rows = legacy.execute(
            "SELECT identity, endpoint, status, payload, error, fetched_at FROM lastfm_evidence"
        ).fetchall()
    except sqlite3.Error as exc:
        raise SafetyError(f"Legacy Last.fm cache is invalid: {exc}") from exc
    finally:
        legacy.close()
    db = connect(state_file)
    try:
        db.execute("BEGIN IMMEDIATE")
        for identity, endpoint, status, payload, error, fetched_at in rows:
            try:
                expires_at = (datetime.fromisoformat(fetched_at) + timedelta(days=90)).isoformat()
            except (TypeError, ValueError):
                expires_at = None
            db.execute(
                """INSERT INTO provider_cache(
                    provider, endpoint, request_key, status, response_json, error,
                    fetched_at, expires_at, attempts
                ) VALUES ('lastfm', ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(provider, endpoint, request_key) DO UPDATE SET
                    status=excluded.status, response_json=excluded.response_json,
                    error=excluded.error, fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at""",
                (endpoint, identity, status, payload, error, fetched_at, expires_at),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return {"imported": len(rows)}
