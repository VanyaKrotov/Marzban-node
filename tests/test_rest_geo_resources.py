import asyncio
import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException

from geo_resource_service import GeoResourceManager


class RestGeoResourceEndpointTest(unittest.TestCase):
    def test_geo_resources_require_valid_session(self):
        with patch("subprocess.check_output", return_value=b"Xray 1.8.0 test"):
            rest_service = importlib.import_module("rest_service")
            service = rest_service.Service()

        service.session_id = uuid4()

        with self.assertRaises(HTTPException) as ctx:
            service.list_geo_resources(session_id=uuid4())

        self.assertEqual(ctx.exception.status_code, 403)

    def test_upload_and_download_stream_binary_content(self):
        with patch("subprocess.check_output", return_value=b"Xray 1.8.0 test"):
            rest_service = importlib.import_module("rest_service")
            service = rest_service.service

        with tempfile.TemporaryDirectory() as directory:
            original_manager = rest_service.geo_resource_manager
            rest_service.geo_resource_manager = GeoResourceManager(assets_path=directory)
            try:
                session_id = uuid4()
                service.session_id = session_id
                upload = asyncio.run(
                    service.upload_geo_resource(
                        _ChunkedRequest([b"first-", b"second"]),
                        session_id=session_id,
                        filename="geoip.dat",
                    )
                )
                self.assertEqual(upload["size"], len(b"first-second"))

                response = service.download_geo_resource(
                    session_id=session_id, filename="geoip.dat"
                )
                content = asyncio.run(_read_response(response))
                self.assertEqual(content, b"first-second")
                self.assertEqual(Path(directory, "geoip.dat").read_bytes(), b"first-second")
            finally:
                rest_service.geo_resource_manager = original_manager


class _ChunkedRequest:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def stream(self):
        for chunk in self.chunks:
            yield chunk


async def _read_response(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    return b"".join(chunks)


if __name__ == "__main__":
    unittest.main()
