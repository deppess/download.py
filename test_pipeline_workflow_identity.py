import json
import tempfile
import unittest
from pathlib import Path

from pipeline_workflow import _source_artist


class WorkflowIdentityTests(unittest.TestCase):
    def test_source_artist_never_uses_uploader_as_artist(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.m4a"
            raw.with_suffix(".info.json").write_text(
                json.dumps({"artist": "Performer", "uploader": "Channel"}), encoding="utf-8"
            )
            self.assertEqual(_source_artist(raw), ("Performer", "Channel"))
