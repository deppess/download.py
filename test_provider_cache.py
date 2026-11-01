import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import provider_cache
import pipeline_state


class ProviderCacheTests(unittest.TestCase):
    def test_request_keys_are_order_independent(self):
        self.assertEqual(
            provider_cache.request_key({"artist": "A", "track": "B"}),
            provider_cache.request_key({"track": "B", "artist": "A"}),
        )

    def test_cache_round_trip_and_attempt_count(self):
        with tempfile.TemporaryDirectory() as directory:
            db = pipeline_state.connect(Path(directory) / "state.sqlite3")
            key = provider_cache.request_key({"artist": "A"})
            future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            provider_cache.put(db, "lastfm", "artist", key, "ok", "{}", expires_at=future)
            provider_cache.put(db, "lastfm", "artist", key, "ok", "{}", expires_at=future)
            db.commit()
            entry = provider_cache.get(db, "lastfm", "artist", key)
            self.assertIsNotNone(entry)
            self.assertEqual(entry.attempts, 2)
            self.assertTrue(entry.fresh)
            db.close()

    def test_retry_entry_is_not_fresh_but_becomes_due(self):
        with tempfile.TemporaryDirectory() as directory:
            db = pipeline_state.connect(Path(directory) / "state.sqlite3")
            key = provider_cache.request_key({"artist": "A"})
            provider_cache.put(
                db,
                "lastfm",
                "artist",
                key,
                "network_error",
                None,
                retry_after_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
            )
            db.commit()
            entry = provider_cache.get(db, "lastfm", "artist", key)
            self.assertFalse(entry.fresh)
            self.assertTrue(entry.retry_due)
            db.close()
