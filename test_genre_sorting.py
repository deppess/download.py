import unittest

import download


class GenreScoringTests(unittest.TestCase):
    def test_pop_is_not_also_electronic(self):
        self.assertEqual(download.score_tags([("pop", 80)]), {"Pop": 80.0})

    def test_jazz_is_not_also_soul_or_oldies(self):
        self.assertEqual(download.score_tags([("jazz", 80)]), {"Jazz": 80.0})

    def test_ambiguous_tags_do_not_choose_a_folder(self):
        self.assertEqual(download.score_tags([("ambient", 100)]), {})

    def test_tied_evidence_is_sent_for_review(self):
        self.assertEqual(
            download.assign_genre({"Rock & Metal": 60, "Electronic": 60}),
            ("Misc", "misc"),
        )

    def test_classical_metadata_beats_a_bad_artist_credit(self):
        self.assertEqual(
            download.metadata_genre_hint(
                "Release", "Requiem in D Minor, K. 626: Introitus. Requiem"
            ),
            ("Classical", "metadata:classical"),
        )

    def test_misc_is_retried_on_a_later_run(self):
        self.assertNotIn("Misc", download.VALID_GENRES)


if __name__ == "__main__":
    unittest.main()
