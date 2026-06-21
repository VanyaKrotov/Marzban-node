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

    def test_import_certificate_requires_valid_session(self):
        with patch("subprocess.check_output", return_value=b"Xray 1.8.0 test"):
            import rest_service

            service = rest_service.Service()
        service.session_id = uuid4()

        with self.assertRaises(HTTPException) as ctx:
            service.import_certificate(
                session_id=uuid4(),
                domain="node.example.com",
                certificate_file="/etc/letsencrypt/live/node.example.com/fullchain.pem",
                key_file="/etc/letsencrypt/live/node.example.com/privkey.pem",
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_import_certificate_calls_certificate_manager(self):
        with patch("subprocess.check_output", return_value=b"Xray 1.8.0 test"):
            import rest_service

            service = rest_service.Service()
        service.session_id = uuid4()

        expected = {
            "certificate": "cert",
            "private_key": "key",
            "certificate_file": "/etc/cert.pem",
            "key_file": "/etc/key.pem",
            "expires_at": "2026-12-31T23:59:59Z",
        }
        with patch.object(
            rest_service.certificate_manager,
            "import_certificate",
            return_value=expected,
        ) as import_certificate:
            result = service.import_certificate(
                session_id=service.session_id,
                domain="node.example.com",
                certificate_file="/etc/cert.pem",
                key_file="/etc/key.pem",
            )

        self.assertEqual(result, expected)
        import_certificate.assert_called_once_with(
            domain="node.example.com",
            certificate_file="/etc/cert.pem",
            key_file="/etc/key.pem",
        )


if __name__ == "__main__":
    unittest.main()
