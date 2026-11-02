import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import music_adapter


class MusicAdapterTests(unittest.TestCase):
    def test_add_to_music_parses_returned_identity_and_location(self):
        with tempfile.TemporaryDirectory() as directory:
            track = Path(directory) / "track.m4a"
            track.write_bytes(b"test")

            class Result:
                returncode = 0
                stdout = "ABC123\x1e" + str(track) + "\n"
                stderr = ""

            with patch.object(music_adapter.sys, "platform", "darwin"):
                persistent_id, location = music_adapter.add_to_music(
                    track, runner=lambda *_args, **_kwargs: Result()
                )
            self.assertEqual(persistent_id, "ABC123")
            self.assertEqual(location, track.resolve())

    def test_import_requires_confirmation(self):
        self.assertEqual(music_adapter.main(["import", "--plan", "x.json"]), 2)

    def test_lookup_returns_none_when_music_has_no_matching_file(self):
        with tempfile.TemporaryDirectory() as directory:
            track = Path(directory) / "track.m4a"
            track.write_bytes(b"test")

            class Result:
                returncode = 0
                stdout = "\n"
                stderr = ""

            with patch.object(music_adapter.sys, "platform", "darwin"):
                self.assertIsNone(
                    music_adapter.find_in_music(track, runner=lambda *_args, **_kwargs: Result())
                )

    def test_same_local_file_accepts_case_normalized_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "Folder" / "track.m4a"
            actual.parent.mkdir()
            actual.write_bytes(b"test")
            self.assertTrue(music_adapter._same_local_file(actual, root / "folder" / "track.m4a"))
