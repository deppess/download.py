import tempfile
import unittest
from pathlib import Path

import pipeline_state
from pipeline_workflow import (
    approve_plan,
    approve_review_item,
    create_plan,
    load_plan,
    write_review,
)


class WorkflowPlanTests(unittest.TestCase):
    def _raw(self, db, digest, status="ready"):
        db.execute(
            """INSERT INTO media_files(role, relative_path, sha256, size, readable, artist, title, album,
               genre, review_status) VALUES ('raw', ?, ?, 1, 1, 'Artist', 'Title', 'Album', 'Electronic', ?)""",
            (f"raw/{digest[:2]}/{digest}.m4a", digest, status),
        )

    def test_plan_is_immutable_and_requires_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, library, plan = root / "state.sqlite3", root / "Library", root / "plan.json"
            library.mkdir()
            db = pipeline_state.connect(state)
            self._raw(db, "a" * 64)
            db.commit()
            db.close()
            result = create_plan(state, library, plan)
            document = load_plan(plan)
            self.assertEqual(len(document["items"]), 1)
            self.assertEqual(approve_plan(state, plan), 1)
            plan.write_text(plan.read_text().replace("Electronic", "Pop"), encoding="utf-8")
            with self.assertRaises(Exception):
                load_plan(plan)
            self.assertTrue(result["plan_id"])

    def test_review_export_contains_only_nonready_raw_items(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state, report = root / "state.sqlite3", root / "review.csv"
            db = pipeline_state.connect(state)
            self._raw(db, "a" * 64, "ready")
            self._raw(db, "b" * 64, "review")
            db.commit()
            db.close()
            self.assertEqual(write_review(state, report), 1)
            self.assertIn("b" * 64, report.read_text(encoding="utf-8"))

    def test_manual_review_approval_requires_existing_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.sqlite3"
            db = pipeline_state.connect(state)
            self._raw(db, "c" * 64, "review")
            media_id = db.execute("SELECT id FROM media_files").fetchone()[0]
            db.commit()
            db.close()
            approve_review_item(state, media_id, "Pop")
            db = pipeline_state.connect(state)
            row = db.execute(
                "SELECT genre, review_status FROM media_files WHERE id=?", (media_id,)
            ).fetchone()
            self.assertEqual((row["genre"], row["review_status"]), ("Pop", "ready"))
            db.close()
