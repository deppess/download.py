import hashlib
import tempfile
import unittest
from pathlib import Path

import library_safety


class LibrarySafetyTests(unittest.TestCase):
    def test_sha256_is_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.m4a"
            path.write_bytes(b"music bytes")
            self.assertEqual(
                library_safety.sha256(path),
                hashlib.sha256(b"music bytes").hexdigest(),
            )

    def test_atomic_copy_keeps_source_and_matches_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.m4a"
            destination = root / "nested" / "copy.m4a"
            source.write_bytes(b"unaltered original")
            expected = library_safety.sha256(source)

            library_safety.atomic_verified_copy(source, destination, expected)

            self.assertEqual(source.read_bytes(), b"unaltered original")
            self.assertEqual(library_safety.sha256(destination), expected)

    def test_atomic_copy_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.m4a"
            destination = root / "copy.m4a"
            source.write_bytes(b"source")
            destination.write_bytes(b"existing")

            with self.assertRaises(library_safety.SafetyError):
                library_safety.atomic_verified_copy(
                    source, destination, library_safety.sha256(source)
                )
            self.assertEqual(destination.read_bytes(), b"existing")

    def test_verification_reports_missing_and_unexpected_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.jsonl"
            manifest.write_text(
                '{"relative_path":"missing.m4a","size":1,"sha256":"x"}\n',
                encoding="utf-8",
            )
            (root / "extra.m4a").write_bytes(b"x")

            errors = library_safety.verify_manifest(root, manifest)

            self.assertIn("missing: missing.m4a", errors)
            self.assertIn("unexpected: extra.m4a", errors)

    def test_package_copy_preserves_every_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Music Library.musiclibrary"
            destination = root / "backup" / source.name
            (source / "nested").mkdir(parents=True)
            (source / "Library.musicdb").write_bytes(b"database")
            (source / "nested" / "preferences.plist").write_bytes(b"settings")

            count, size = library_safety._copy_package(source, destination)

            self.assertEqual((count, size), (2, len(b"database") + len(b"settings")))
            self.assertEqual((destination / "Library.musicdb").read_bytes(), b"database")
            self.assertEqual(
                (destination / "nested" / "preferences.plist").read_bytes(), b"settings"
            )


if __name__ == "__main__":
    unittest.main()
