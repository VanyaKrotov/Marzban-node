import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException


class RestCertificateEndpointTest(unittest.TestCase):
    def test_issue_certificate_requires_valid_session(self):
        with patch("subprocess.check_output", return_value=b"Xray 1.8.0 test"):
            import rest_service

            service = rest_service.Service()
        service.session_id = uuid4()

        with self.assertRaises(HTTPException) as ctx:
            service.issue_certificate(
                session_id=uuid4(),
                domain="node.example.com",
            )

        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
