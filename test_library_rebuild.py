import json
import tempfile
import unittest
from pathlib import Path

import library_rebuild


class RebuildPlanTests(unittest.TestCase):
    def test_plan_keeps_uncertain_tracks_in_review_and_detects_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, plan, review = (
                root / "state.db",
                root / "plan.jsonl",
                root / "review.csv",
            )
            records = [
                {
                    "relative_path": "A/one.m4a",
                    "sha256": "a",
                    "artist": "Artist",
                    "title": "One",
                    "album": "Album",
                },
                {
                    "relative_path": "B/one.m4a",
                    "sha256": "b",
                    "artist": "Artist",
                    "title": "Two",
                    "album": "Album",
                },
            ]
            result = library_rebuild.create_plan(records, state, plan, review)
            rows = [json.loads(line) for line in plan.read_text().splitlines()]
            self.assertEqual(result["tracks"], 2)
            self.assertEqual(result["ready"], 0)
            self.assertTrue(all(row["status"] == "review" for row in rows))
            self.assertTrue(review.is_file())

    def test_safe_component_removes_path_separators(self):
        self.assertEqual(library_rebuild._safe_component(" A/B:C ", "x"), "A_B_C")

    def test_stage_refuses_nonempty_destination_before_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "library-manifest.jsonl").write_text("", encoding="utf-8")
            destination = root / "destination"
            destination.mkdir()
            (destination / "keep.txt").write_text("do not overwrite", encoding="utf-8")
            plan = root / "plan.jsonl"
            plan.write_text(
                '{"source":"one.m4a","destination":"Review/one.m4a","sha256":"x"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(library_rebuild.SafetyError):
                library_rebuild.stage_plan(plan, source, destination)
            self.assertEqual((destination / "keep.txt").read_text(), "do not overwrite")

    def test_stage_command_does_not_require_manifest_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "source", root / "destination"
            source.mkdir()
            (source / "library-manifest.jsonl").write_text("", encoding="utf-8")
            plan = root / "plan.jsonl"
            plan.write_text("", encoding="utf-8")
            self.assertEqual(
                library_rebuild.main(
                    [
                        "stage",
                        "--plan",
                        str(plan),
                        "--source",
                        str(source),
                        "--destination",
                        str(destination),
                        "--confirm-stage-copy",
                    ]
                ),
                2,
            )
