import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pipeline_state
import provider_cache
from provider_clients import LastFMClient, LRCLIBClient, ProviderConfigurationError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class ProviderClientTests(unittest.TestCase):
    def test_lastfm_reuses_migrated_identity_cache_without_a_network_request(self):
        with tempfile.TemporaryDirectory() as directory:
            db = pipeline_state.connect(Path(directory) / "state.sqlite3")
            key = json.dumps(
                ("Artist", "Track", "Album"), ensure_ascii=False, separators=(",", ":")
            )
            provider_cache.put(
                db,
                "lastfm",
                "track",
                key,
                "ok",
                '{"toptags":{"tag":[]}}',
                expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            )
            db.commit()
            client = LastFMClient(
                db, api_key="test", opener=lambda *_args, **_kwargs: self.fail("network used")
            )
            result = client.top_tags("track", "Artist", "Track", "Album")
            self.assertTrue(result.cached)
            self.assertEqual(result.status, "ok")
            db.close()

    def test_lastfm_api_error_is_cached_for_retry_not_raised(self):
        with tempfile.TemporaryDirectory() as directory:
            db = pipeline_state.connect(Path(directory) / "state.sqlite3")
            client = LastFMClient(
                db,
                api_key="test",
                opener=lambda *_args, **_kwargs: FakeResponse(
                    {"error": 6, "message": "Invalid parameters"}
                ),
            )
            result = client.top_tags("artist", "Artist")
            self.assertEqual(result.status, "api_error")
            key = json.dumps(("Artist", "", ""), separators=(",", ":"))
            entry = provider_cache.get(db, "lastfm", "artist", key)
            self.assertIsNotNone(entry)
            self.assertFalse(entry.fresh)
            db.close()

    def test_lrclib_response_is_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            db = pipeline_state.connect(Path(directory) / "state.sqlite3")
            requests = []

            def opener(request, **_kwargs):
                requests.append(request.full_url)
                return FakeResponse({"plainLyrics": "words"})

            client = LRCLIBClient(db, opener=opener)
            self.assertFalse(client.lyrics("Artist", "Track").cached)
            self.assertTrue(client.lyrics("Artist", "Track").cached)
            self.assertEqual(len(requests), 1)
            db.close()

    def test_lastfm_requires_an_explicit_or_environment_key(self):
        with tempfile.TemporaryDirectory() as directory:
            db = pipeline_state.connect(Path(directory) / "state.sqlite3")
            with patch.dict("os.environ", {"LASTFM_API_KEY": ""}):
                with self.assertRaises(ProviderConfigurationError):
                    LastFMClient(db, api_key="")
            db.close()
