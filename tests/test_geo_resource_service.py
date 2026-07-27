import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from geo_resource_service import (
    GeoResourceConflictError,
    GeoResourceInputError,
    GeoResourceManager,
    GeoResourceTooLargeError,
)


class GeoResourceManagerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.manager = GeoResourceManager(assets_path=self.temporary.name, max_size=8)

    def upload(self, filename: str, chunks: list[bytes], overwrite: bool = False):
        token = self.manager.begin_upload(filename, overwrite=overwrite)
        try:
            for chunk in chunks:
                self.manager.append_upload(token, chunk)
            return self.manager.finish_upload(token)
        except Exception:
            self.manager.abort_upload(token)
            raise

    def test_filename_validation_rejects_traversal_and_non_dat_files(self):
        invalid = ["", ".", "..", "../geoip.dat", "sub/geosite.dat", "sub\\geosite.dat", "/tmp/geoip.dat", "geoip.db"]
        for filename in invalid:
            with self.subTest(filename=filename):
                with self.assertRaises(GeoResourceInputError):
                    self.manager.validate_filename(filename)

    def test_upload_and_download_are_chunked(self):
        uploaded = self.upload("geoip.dat", [b"con", b"tent"])

        self.assertEqual(uploaded["filename"], "geoip.dat")
        self.assertEqual(uploaded["size"], 7)
        self.assertEqual(self.manager.list_resources()["files"][0]["filename"], "geoip.dat")
        self.assertEqual(list(self.manager.iter_resource("geoip.dat", chunk_size=3)), [b"con", b"ten", b"t"])

    def test_list_and_download_reject_symlinks(self):
        (self.directory / "notes.txt").write_text("ignore")
        (self.directory / "geoip.dat").write_bytes(b"data")
        link = self.directory / "linked.dat"
        try:
            link.symlink_to(self.directory / "geoip.dat")
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable")

        self.assertEqual([item["filename"] for item in self.manager.list_resources()["files"]], ["geoip.dat"])
        with self.assertRaises(GeoResourceInputError):
            self.manager.iter_resource("linked.dat")

    def test_upload_conflict_and_atomic_overwrite(self):
        self.upload("geoip.dat", [b"old"])
        with self.assertRaises(GeoResourceConflictError):
            self.upload("geoip.dat", [b"new"])

        with patch("geo_resource_service.os.replace", wraps=os.replace) as replace:
            self.upload("geoip.dat", [b"n", b"ew"], overwrite=True)

        replace.assert_called_once()
        self.assertEqual((self.directory / "geoip.dat").read_bytes(), b"new")
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_oversized_or_aborted_upload_keeps_existing_file(self):
        self.upload("geoip.dat", [b"old"])
        token = self.manager.begin_upload("geoip.dat", overwrite=True)
        with self.assertRaises(GeoResourceTooLargeError):
            self.manager.append_upload(token, b"123456789")
        self.manager.abort_upload(token)

        self.assertEqual((self.directory / "geoip.dat").read_bytes(), b"old")
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_download_rejects_large_file(self):
        (self.directory / "geoip.dat").write_bytes(b"123456789")
        with self.assertRaises(GeoResourceTooLargeError):
            self.manager.iter_resource("geoip.dat")

    def test_rename_conflict_and_overwrite(self):
        (self.directory / "source.dat").write_bytes(b"source")
        (self.directory / "target.dat").write_bytes(b"target")
        with self.assertRaises(GeoResourceConflictError):
            self.manager.rename_resource("source.dat", "target.dat")

        result = self.manager.rename_resource("source.dat", "target.dat", overwrite=True)
        self.assertEqual(result["filename"], "target.dat")
        self.assertEqual((self.directory / "target.dat").read_bytes(), b"source")

    def test_delete_validates_complete_list_before_deleting(self):
        target = self.directory / "geoip.dat"
        target.write_bytes(b"data")
        with self.assertRaises(GeoResourceInputError):
            self.manager.delete_resources(["geoip.dat", "../bad.dat"])
        self.assertTrue(target.exists())


if __name__ == "__main__":
    unittest.main()
