import json
import tempfile
import unittest
from pathlib import Path

import pipeline_intake


class IntakeHelpersTests(unittest.TestCase):
    def test_source_details_reads_stable_id_and_url_from_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "Track [abcdefghijk].m4a"
            sidecar = root / "Track [abcdefghijk].info.json"
            sidecar.write_text(
                json.dumps({"id": "abcdefghijk", "webpage_url": "https://example.test/v"})
            )
            self.assertEqual(
                pipeline_intake.source_details(media, sidecar),
                ("abcdefghijk", "https://example.test/v"),
            )

    def test_source_details_falls_back_to_filename_id_without_sidecar(self):
        media = Path("Track [abcdefghijk].m4a")
        self.assertEqual(
            pipeline_intake.source_details(media, Path("missing.info.json")), ("abcdefghijk", None)
        )
