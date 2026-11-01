import os
import unittest
from unittest.mock import patch

import music_paths


class MusicPathsTests(unittest.TestCase):
    def test_environment_root_controls_all_pipeline_paths(self):
        with patch.dict(os.environ, {"DOWNLOAD_MUSIC_ROOT": "/tmp/music-root"}):
            paths = music_paths.load_paths()
        self.assertEqual(str(paths.root), "/tmp/music-root")
        self.assertEqual(str(paths.library), "/tmp/music-root/Library")
        self.assertEqual(str(paths.landing), "/tmp/music-root/download-staging")
        self.assertEqual(str(paths.state), "/tmp/music-root/.download-state")
