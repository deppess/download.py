import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import music_paths


class ConfigurationTests(unittest.TestCase):
    def test_toml_root_is_used_when_environment_does_not_override(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[paths]\nroot = "~/Example Music"\n', encoding="utf-8")
            with patch.dict(os.environ, {"DOWNLOAD_CONFIG": str(config)}, clear=False):
                os.environ.pop("DOWNLOAD_MUSIC_ROOT", None)
                paths = music_paths.load_paths()
            self.assertEqual(paths.root, Path.home() / "Example Music")

    def test_environment_root_has_precedence_over_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.toml"
            config.write_text('[paths]\nroot = "/configured"\n', encoding="utf-8")
            with patch.dict(
                os.environ, {"DOWNLOAD_CONFIG": str(config), "DOWNLOAD_MUSIC_ROOT": "/environment"}
            ):
                self.assertEqual(music_paths.load_paths().root, Path("/environment"))
