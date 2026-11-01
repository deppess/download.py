import unittest
from unittest.mock import patch

import download


class DownloadSafetyTests(unittest.TestCase):
    def test_download_template_uses_stable_id_not_uploader_identity(self):
        output_index = download._YTDLP_DOWNLOAD_ARGS.index("--output") + 1
        self.assertIn("%(id)s", download._YTDLP_DOWNLOAD_ARGS[output_index])
        self.assertNotIn("%(uploader)s", download._YTDLP_DOWNLOAD_ARGS[output_index])
        self.assertIn("incoming", download._YTDLP_DOWNLOAD_ARGS[output_index])

    def test_ytdlp_never_maps_uploader_into_artist_metadata(self):
        self.assertNotIn("--parse-metadata", download._YTDLP_DOWNLOAD_ARGS)
        self.assertNotIn("--replace-in-metadata", download._YTDLP_DOWNLOAD_ARGS)

    def test_ytdlp_writes_private_source_sidecar_metadata(self):
        self.assertIn("--write-info-json", download._YTDLP_DOWNLOAD_ARGS)
        self.assertIn("--no-write-playlist-metafiles", download._YTDLP_DOWNLOAD_ARGS)

    def test_low_confidence_genre_tags_are_not_written_by_default(self):
        # The policy is represented in tag_files and intentionally exposed as
        # an explicit opt-in flag for legacy behavior.
        self.assertIn("write_low_confidence", download.tag_files.__code__.co_varnames)

    def test_public_entrypoint_dispatches_intake_before_legacy_parser(self):
        with patch("pipeline_intake.main", return_value=0) as intake:
            self.assertEqual(download.main(["intake", "--help"]), 0)
        intake.assert_called_once_with(["--help"])

    def test_fetch_alias_reuses_safe_fetch_only_legacy_path(self):
        with patch.object(download, "legacy_main", return_value=0) as legacy:
            self.assertEqual(download.main(["fetch", "playlist"]), 0)
        legacy.assert_called_once_with(["playlist"])
