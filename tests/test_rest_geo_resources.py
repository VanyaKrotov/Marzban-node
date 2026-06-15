import importlib
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException


class RestGeoResourceEndpointTest(unittest.TestCase):
    def test_geo_resources_require_valid_session(self):
        with patch("subprocess.check_output", return_value=b"Xray 1.8.0 test"):
            rest_service = importlib.import_module("rest_service")
            service = rest_service.Service()

        service.session_id = uuid4()

        with self.assertRaises(HTTPException) as ctx:
            service.list_geo_resources(session_id=uuid4())

        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
