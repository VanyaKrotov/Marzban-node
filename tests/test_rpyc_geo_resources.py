import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from geo_resource_service import GeoResourceManager
from rpyc_service import XrayService


class RpycGeoResourceTest(unittest.TestCase):
    def test_upload_session_streams_and_can_be_aborted(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = GeoResourceManager(assets_path=directory)
            service = XrayService()
            service.connection = object()

            with patch("rpyc_service.geo_resource_manager", manager):
                token = service.begin_geo_resource_upload("geoip.dat")
                service.append_geo_resource_upload(token, b"first-")
                service.append_geo_resource_upload(token, b"second")
                uploaded = service.finish_geo_resource_upload(token)

                self.assertEqual(uploaded["size"], len(b"first-second"))
                self.assertEqual(
                    list(service.download_geo_resource("geoip.dat")), [b"first-second"]
                )

                token = service.begin_geo_resource_upload("geosite.dat")
                service.append_geo_resource_upload(token, b"partial")
                service.abort_geo_resource_upload(token)

            self.assertFalse(Path(directory, "geosite.dat").exists())
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
