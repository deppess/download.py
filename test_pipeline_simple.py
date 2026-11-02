import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline_simple


class SimplePipelineTests(unittest.TestCase):
    def test_review_items_are_promoted_to_review_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.sqlite3"
            db = pipeline_simple.connect(state)
            db.execute(
                "INSERT INTO media_files(role, relative_path, sha256, size, readable, review_status) VALUES ('raw', 'raw/a.m4a', ?, 1, 1, 'review')",
                ("a" * 64,),
            )
            db.commit()
            db.close()
            self.assertEqual(pipeline_simple.mark_review_for_simple_import(state), 1)
            db = pipeline_simple.connect(state)
            row = db.execute("SELECT genre, review_status FROM media_files").fetchone()
            self.assertEqual((row["genre"], row["review_status"]), ("Review", "ready"))
            db.close()

    def test_legacy_config_key_is_used_when_environment_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.ini"
            config.write_text("[lastfm]\napi_key = legacy-key\n", encoding="utf-8")
            with patch.dict("os.environ", {"LASTFM_API_KEY": ""}):
                self.assertEqual(pipeline_simple._lastfm_key(config), "legacy-key")

    def test_no_pending_imports_returns_zero_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.sqlite3"
            pipeline_simple.connect(state).close()
            self.assertEqual(
                pipeline_simple.resume_pending_music_imports(state, Path(directory) / "Library"),
                {"reconciled": 0, "music_imported": 0, "music_already_present": 0},
            )
