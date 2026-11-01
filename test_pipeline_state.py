import sqlite3
import tempfile
import unittest
from pathlib import Path

import pipeline_state


class PipelineStateTests(unittest.TestCase):
    def test_connect_creates_versioned_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db = pipeline_state.connect(Path(directory) / "state.sqlite3")
            self.assertEqual(
                db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 4
            )
            self.assertIn(
                "media_files",
                {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
            )
            db.close()

    def test_state_summary_uses_empty_state_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = pipeline_state.state_summary(Path(directory) / "state.sqlite3")
            self.assertEqual(
                summary,
                {
                    "library_tracks": 0,
                    "review_tracks": 0,
                    "sources": 0,
                    "cached_provider_responses": 0,
                    "raw_tracks": 0,
                    "pending_plan_items": 0,
                },
            )

    def test_import_legacy_lastfm_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy_path, state_path = root / "legacy.sqlite3", root / "state.sqlite3"
            legacy = sqlite3.connect(legacy_path)
            legacy.execute(
                "CREATE TABLE lastfm_evidence (identity TEXT, endpoint TEXT, status TEXT, payload TEXT, error TEXT, fetched_at TEXT)"
            )
            legacy.execute(
                "INSERT INTO lastfm_evidence VALUES (?, ?, ?, ?, ?, ?)",
                (
                    '["Artist","Title","Album"]',
                    "track",
                    "ok",
                    "{}",
                    None,
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            legacy.commit()
            legacy.close()
            self.assertEqual(
                pipeline_state.import_legacy_lastfm_cache(legacy_path, state_path), {"imported": 1}
            )
            self.assertEqual(
                pipeline_state.state_summary(state_path)["cached_provider_responses"], 1
            )

    def test_writer_lock_releases_after_context(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            with pipeline_state.writer_lock(state_dir):
                self.assertTrue((state_dir / "locks" / "writer.lock").is_file())
            with pipeline_state.writer_lock(state_dir):
                pass
