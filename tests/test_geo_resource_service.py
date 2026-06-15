import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from geo_resource_service import (GeoResourceConflictError,
                                  GeoResourceInputError,
                                  GeoResourceManager,
                                  GeoResourceTooLargeError)


class GeoResourceManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.manager = GeoResourceManager(
            assets_path=self.temporary.name,
            max_size=8,
        )

    @staticmethod
    def encode(content: bytes) -> str:
        return base64.b64encode(content).decode("ascii")

    def test_filename_validation_rejects_traversal_and_non_dat_files(self):
        invalid = [
            "",
            ".",
            "..",
            "../geoip.dat",
            "sub/geosite.dat",
            "sub\\geosite.dat",
            "/tmp/geoip.dat",
            "geoip.db",
        ]
        for filename in invalid:
            with self.subTest(filename=filename):
                with self.assertRaises(GeoResourceInputError):
                    self.manager.validate_filename(filename)

    def test_upload_list_and_download(self):
        uploaded = self.manager.upload_resource(
            "geoip.dat",
            self.encode(b"content"),
        )

        self.assertEqual(uploaded["filename"], "geoip.dat")
        self.assertEqual(uploaded["size"], 7)
        listed = self.manager.list_resources()
        self.assertEqual(listed["files"][0]["filename"], "geoip.dat")
        self.assertTrue(listed["files"][0]["modified_at"].endswith("Z"))
        downloaded = self.manager.download_resource("geoip.dat")
        self.assertEqual(base64.b64decode(downloaded["content"]), b"content")

    def test_list_ignores_non_dat_and_symlinks(self):
        (self.directory / "notes.txt").write_text("ignore")
        (self.directory / "geoip.dat").write_bytes(b"data")
        link = self.directory / "linked.dat"
        try:
            link.symlink_to(self.directory / "geoip.dat")
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")

        filenames = [
            item["filename"] for item in self.manager.list_resources()["files"]
        ]

        self.assertEqual(filenames, ["geoip.dat"])
        with self.assertRaises(GeoResourceInputError):
            self.manager.download_resource("linked.dat")

    def test_upload_conflict_and_atomic_overwrite(self):
        self.manager.upload_resource("geoip.dat", self.encode(b"old"))

        with self.assertRaises(GeoResourceConflictError):
            self.manager.upload_resource("geoip.dat", self.encode(b"new"))

        with patch("geo_resource_service.os.replace", wraps=os.replace) as replace:
            self.manager.upload_resource(
                "geoip.dat",
                self.encode(b"new"),
                overwrite=True,
            )

        replace.assert_called_once()
        self.assertEqual((self.directory / "geoip.dat").read_bytes(), b"new")
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_upload_rejects_invalid_base64_and_size_limit(self):
        with self.assertRaises(GeoResourceInputError):
            self.manager.upload_resource("geoip.dat", "not base64!")

        with self.assertRaises(GeoResourceTooLargeError):
            self.manager.upload_resource(
                "geoip.dat",
                self.encode(b"123456789"),
            )

    def test_download_rejects_large_file(self):
        (self.directory / "geoip.dat").write_bytes(b"123456789")

        with self.assertRaises(GeoResourceTooLargeError):
            self.manager.download_resource("geoip.dat")

    def test_rename_conflict_and_overwrite(self):
        (self.directory / "source.dat").write_bytes(b"source")
        (self.directory / "target.dat").write_bytes(b"target")

        with self.assertRaises(GeoResourceConflictError):
            self.manager.rename_resource("source.dat", "target.dat")

        result = self.manager.rename_resource(
            "source.dat",
            "target.dat",
            overwrite=True,
        )

        self.assertEqual(result["filename"], "target.dat")
        self.assertEqual((self.directory / "target.dat").read_bytes(), b"source")
        self.assertFalse((self.directory / "source.dat").exists())

    def test_delete_validates_complete_list_before_deleting(self):
        target = self.directory / "geoip.dat"
        target.write_bytes(b"data")

        with self.assertRaises(GeoResourceInputError):
            self.manager.delete_resources(["geoip.dat", "../bad.dat"])

        self.assertTrue(target.exists())

    def test_delete_is_idempotent_for_missing_files(self):
        (self.directory / "geoip.dat").write_bytes(b"data")

        result = self.manager.delete_resources(
            ["geoip.dat", "already-missing.dat"]
        )
        second = self.manager.delete_resources(["geoip.dat"])

        self.assertEqual(
            result["filenames"],
            ["geoip.dat", "already-missing.dat"],
        )
        self.assertEqual(second["filenames"], ["geoip.dat"])


if __name__ == "__main__":
    unittest.main()
