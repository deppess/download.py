import unittest

from music_taxonomy import TAXONOMY_VERSION, classify_tag_evidence


class TaxonomyTests(unittest.TestCase):
    def test_track_evidence_can_classify_a_known_bucket(self):
        result = classify_tag_evidence([("track", "ok", [{"name": "techno", "count": 50}])])
        self.assertEqual(result.genre, "Electronic")
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.taxonomy_version, TAXONOMY_VERSION)

    def test_artist_only_evidence_requires_review(self):
        result = classify_tag_evidence([("artist", "ok", [{"name": "rap", "count": 100}])])
        self.assertEqual(result.genre, "Review")
        self.assertEqual(result.status, "review")

    def test_conflicting_evidence_requires_review(self):
        result = classify_tag_evidence(
            [("track", "ok", [{"name": "techno", "count": 20}, {"name": "rap", "count": 20}])]
        )
        self.assertEqual(result.genre, "Review")

    def test_unknown_or_failed_evidence_is_not_guessed(self):
        result = classify_tag_evidence(
            [("track", "network_error", []), ("album", "ok", [{"name": "obscure"}])]
        )
        self.assertEqual(result.genre, "Review")
