import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from certificate_service import (CertificateInputError, CertificateManager,
                                 CertificateTimeoutError,
                                 CertificateToolError, PortUnavailableError,
                                 normalize_domain)


def generate_pair(domain, expires_in_days=90, key=None):
    key = key or rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=expires_in_days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(domain)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


class FakeAcme:
    def __init__(self, domain="node.example.com", expires_in_days=90):
        self.domain = domain
        self.expires_in_days = expires_in_days
        self.commands = []

    def __call__(self, command, timeout):
        self.commands.append(command)
        if "--install-cert" in command:
            cert_index = command.index("--fullchain-file") + 1
            key_index = command.index("--key-file") + 1
            cert, key = generate_pair(self.domain, self.expires_in_days)
            Path(command[cert_index]).write_bytes(cert)
            Path(command[key_index]).write_bytes(key)


class CertificateManagerTest(unittest.TestCase):
    def make_manager(self, runner=None, renew_before_days=30):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        return CertificateManager(
            storage_dir=self.tmp.name,
            acme_path="/usr/local/bin/acme.sh",
            timeout=5,
            renew_before_days=renew_before_days,
            runner=runner or FakeAcme(),
            port_checker=lambda: None,
        )

    def test_domain_validation_rejects_unsafe_values(self):
        invalid = [
            "*.example.com",
            "127.0.0.1",
            "example.com/path",
            "example.com:443",
            "bad_label.example.com",
            "localhost",
        ]
        for domain in invalid:
            with self.subTest(domain=domain):
                with self.assertRaises(CertificateInputError):
                    normalize_domain(domain)

        self.assertEqual(normalize_domain("Node.Example.Com."), "node.example.com")

    def test_successful_issue_writes_restrictive_files(self):
        runner = FakeAcme()
        manager = self.make_manager(runner=runner)

        result = manager.issue_certificate("node.example.com")

        self.assertEqual(result["domain"], "node.example.com")
        self.assertIn("BEGIN CERTIFICATE", result["certificate"])
        self.assertIn("BEGIN PRIVATE KEY", result["private_key"])
        key_path = Path(self.tmp.name) / "node.example.com" / "production" / "private_key.pem"
        if os.name != "nt":
            self.assertEqual(os.stat(key_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(key_path.parent).st_mode & 0o777, 0o700)
        self.assertEqual(len(runner.commands), 2)

    def test_reuses_existing_valid_certificate(self):
        runner = FakeAcme()
        manager = self.make_manager(runner=runner)

        manager.issue_certificate("node.example.com")
        runner.commands.clear()
        manager.issue_certificate("node.example.com")

        self.assertEqual(runner.commands, [])

    def test_force_renews_existing_certificate(self):
        runner = FakeAcme()
        manager = self.make_manager(runner=runner)

        manager.issue_certificate("node.example.com")
        runner.commands.clear()
        manager.issue_certificate("node.example.com", force=True)

        self.assertIn("--force", runner.commands[0])

    def test_staging_uses_letsencrypt_test_server(self):
        runner = FakeAcme()
        manager = self.make_manager(runner=runner)

        manager.issue_certificate("node.example.com", staging=True)

        self.assertIn("letsencrypt_test", runner.commands[0])

    def test_renews_when_expiration_is_due(self):
        runner = FakeAcme(expires_in_days=3)
        manager = self.make_manager(runner=runner, renew_before_days=30)

        manager.issue_certificate("node.example.com")
        runner.commands.clear()
        manager.issue_certificate("node.example.com")

        self.assertIn("--force", runner.commands[0])

    def test_acme_failure_is_sanitized(self):
        def failing_runner(command, timeout):
            raise CertificateToolError(
                CertificateManager._sanitize_error(
                    "failed -----BEGIN PRIVATE KEY-----secret-----END PRIVATE KEY-----"
                )
            )

        manager = self.make_manager(runner=failing_runner)

        with self.assertRaises(CertificateToolError) as ctx:
            manager.issue_certificate("node.example.com")
        self.assertNotIn("secret", str(ctx.exception))

    def test_timeout_is_reported(self):
        def timeout_runner(command, timeout):
            raise CertificateTimeoutError("ACME operation timed out.")

        manager = self.make_manager(runner=timeout_runner)

        with self.assertRaises(CertificateTimeoutError):
            manager.issue_certificate("node.example.com")

    def test_port_80_unavailable_is_reported(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        manager = CertificateManager(
            storage_dir=temporary.name,
            runner=FakeAcme(),
            port_checker=lambda: (_ for _ in ()).throw(
                PortUnavailableError("TCP port 80 is unavailable.")
            ),
        )

        with self.assertRaises(PortUnavailableError):
            manager.issue_certificate("node.example.com")

    def test_concurrent_same_domain_is_serialized(self):
        calls = []

        def slow_runner(command, timeout):
            calls.append((command, time.time()))
            if "--issue" in command:
                time.sleep(0.05)
            if "--install-cert" in command:
                cert_index = command.index("--fullchain-file") + 1
                key_index = command.index("--key-file") + 1
                cert, key = generate_pair("node.example.com")
                Path(command[cert_index]).write_bytes(cert)
                Path(command[key_index]).write_bytes(key)

        manager = self.make_manager(runner=slow_runner)
        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    manager.issue_certificate("node.example.com")
                )
            )
            for _ in range(2)
        ]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(
            len([command for command, _ in calls if "--issue" in command]),
            1,
        )

    def test_key_certificate_mismatch_is_rejected(self):
        def mismatched_runner(command, timeout):
            if "--install-cert" in command:
                cert_index = command.index("--fullchain-file") + 1
                key_index = command.index("--key-file") + 1
                cert, _ = generate_pair("node.example.com")
                _, key = generate_pair("node.example.com")
                Path(command[cert_index]).write_bytes(cert)
                Path(command[key_index]).write_bytes(key)

        manager = self.make_manager(runner=mismatched_runner)

        with self.assertRaises(CertificateToolError):
            manager.issue_certificate("node.example.com")


@unittest.skipUnless(
    os.environ.get("MARZBAN_NODE_ACME_STAGING_TEST") == "1",
    "set MARZBAN_NODE_ACME_STAGING_TEST=1 to run Let's Encrypt staging test",
)
class AcmeStagingIntegrationTest(unittest.TestCase):
    def test_issue_against_letsencrypt_staging(self):
        domain = os.environ["MARZBAN_NODE_ACME_DOMAIN"]
        email = os.environ.get("MARZBAN_NODE_ACME_EMAIL")
        with tempfile.TemporaryDirectory() as storage:
            manager = CertificateManager(storage_dir=storage)
            result = manager.issue_certificate(domain, email=email, staging=True)
            self.assertEqual(result["domain"], normalize_domain(domain))


if __name__ == "__main__":
    unittest.main()
