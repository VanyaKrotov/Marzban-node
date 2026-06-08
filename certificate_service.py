import ipaddress
import os
import re
import socket
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import idna
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from config import (ACME_RENEW_BEFORE_DAYS, ACME_SH_PATH, ACME_TIMEOUT,
                    CERTIFICATES_DIR)


CERTIFICATE_PATTERN = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
    re.DOTALL,
)
PEM_PATTERN = re.compile(
    r"-----BEGIN [^-]+-----.*?-----END [^-]+-----",
    re.DOTALL,
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CertificateServiceError(Exception):
    status_code = 500


class CertificateInputError(CertificateServiceError):
    status_code = 422


class CertificateToolError(CertificateServiceError):
    status_code = 502


class CertificateTimeoutError(CertificateServiceError):
    status_code = 504


class PortUnavailableError(CertificateServiceError):
    status_code = 503


def normalize_domain(domain: str) -> str:
    if not isinstance(domain, str):
        raise CertificateInputError("Domain must be a string.")

    domain = domain.strip().rstrip(".").lower()
    if not domain:
        raise CertificateInputError("Domain is required.")
    if "*" in domain:
        raise CertificateInputError("Wildcard domains are not supported.")
    if any(character in domain for character in ("/", "\\", ":", "\0")):
        raise CertificateInputError("Domain must be a DNS name, not a path or address.")

    try:
        ipaddress.ip_address(domain)
    except ValueError:
        pass
    else:
        raise CertificateInputError("IP addresses are not supported.")

    try:
        normalized = idna.encode(domain, uts46=True).decode("ascii")
    except idna.IDNAError as exc:
        raise CertificateInputError("Domain is not a valid DNS name.") from exc

    if len(normalized) > 253 or "." not in normalized:
        raise CertificateInputError("Domain is not a valid fully qualified DNS name.")

    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise CertificateInputError("Domain is not a valid DNS name.")

    return normalized


def normalize_email(email: str | None) -> str | None:
    if email is None:
        return None
    if not isinstance(email, str):
        raise CertificateInputError("Email must be a string.")

    email = email.strip()
    if not email:
        return None
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise CertificateInputError("Email address is invalid.")
    return email


class CertificateManager:
    def __init__(
        self,
        storage_dir: str = CERTIFICATES_DIR,
        acme_path: str = ACME_SH_PATH,
        timeout: int = ACME_TIMEOUT,
        renew_before_days: int = ACME_RENEW_BEFORE_DAYS,
        runner=None,
        port_checker=None,
    ):
        self.storage_dir = Path(storage_dir)
        self.acme_path = acme_path
        self.timeout = timeout
        self.renew_before = timedelta(days=renew_before_days)
        self.runner = runner or self._run_command
        self.port_checker = port_checker or self._check_port_80
        self._locks = {}
        self._locks_guard = threading.Lock()
        self._http_lock = threading.Lock()

    def issue_certificate(
        self,
        domain: str,
        email: str | None = None,
        staging: bool = False,
        force: bool = False,
    ) -> dict:
        domain = normalize_domain(domain)
        email = normalize_email(email)

        with self._domain_lock(domain, staging):
            directory = self._certificate_directory(domain, staging)
            certificate_path = directory / "fullchain.pem"
            private_key_path = directory / "private_key.pem"

            if not force and certificate_path.is_file() and private_key_path.is_file():
                try:
                    result = self._load_and_validate(
                        domain, certificate_path, private_key_path
                    )
                except CertificateServiceError:
                    result = None
                if result and not self._renewal_due(result["expires_at"]):
                    return result

            self._issue_with_acme(
                domain=domain,
                email=email,
                staging=staging,
                force=force or certificate_path.exists(),
                directory=directory,
            )
            return self._load_and_validate(
                domain, certificate_path, private_key_path
            )

    def _domain_lock(self, domain: str, staging: bool):
        key = (domain, staging)
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        return lock

    def _certificate_directory(self, domain: str, staging: bool) -> Path:
        environment = "staging" if staging else "production"
        directory = self.storage_dir / domain / environment
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.storage_dir, 0o700)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
        return directory

    def _issue_with_acme(
        self,
        domain: str,
        email: str | None,
        staging: bool,
        force: bool,
        directory: Path,
    ):
        acme_home = directory / ".acme"
        acme_home.mkdir(mode=0o700, exist_ok=True)
        os.chmod(acme_home, 0o700)

        server = "letsencrypt_test" if staging else "letsencrypt"
        common = [
            self.acme_path,
            "--home",
            str(acme_home),
            "--config-home",
            str(acme_home),
            "--server",
            server,
        ]
        issue_command = [
            *common,
            "--issue",
            "--standalone",
            "-d",
            domain,
        ]
        if email:
            issue_command.extend(["--accountemail", email])
        if force:
            issue_command.append("--force")

        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            temporary_dir = Path(temporary)
            temporary_certificate = temporary_dir / "fullchain.pem"
            temporary_key = temporary_dir / "private_key.pem"

            with self._http_lock:
                self.port_checker()
                self.runner(issue_command, self.timeout)
                self.runner(
                    [
                        *common,
                        "--install-cert",
                        "-d",
                        domain,
                        "--key-file",
                        str(temporary_key),
                        "--fullchain-file",
                        str(temporary_certificate),
                    ],
                    self.timeout,
                )

            result = self._load_and_validate(
                domain, temporary_certificate, temporary_key
            )
            self._atomic_write(
                directory / "fullchain.pem",
                result["certificate"].encode("utf-8"),
                0o600,
            )
            self._atomic_write(
                directory / "private_key.pem",
                result["private_key"].encode("utf-8"),
                0o600,
            )

    def _load_and_validate(
        self, domain: str, certificate_path: Path, private_key_path: Path
    ) -> dict:
        try:
            certificate_pem = certificate_path.read_bytes()
            private_key_pem = private_key_path.read_bytes()
        except OSError as exc:
            raise CertificateToolError("Certificate files were not produced.") from exc

        blocks = CERTIFICATE_PATTERN.findall(certificate_pem)
        if not blocks:
            raise CertificateToolError("The certificate chain is not valid PEM.")

        try:
            leaf = x509.load_pem_x509_certificate(blocks[0])
            private_key = serialization.load_pem_private_key(
                private_key_pem, password=None
            )
            san = leaf.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value
        except (ValueError, TypeError, x509.ExtensionNotFound) as exc:
            raise CertificateToolError("The issued certificate is invalid.") from exc

        dns_names = {
            normalize_domain(name)
            for name in san.get_values_for_type(x509.DNSName)
        }
        if domain not in dns_names:
            raise CertificateToolError(
                "The issued certificate does not contain the requested DNS name."
            )

        now = datetime.now(timezone.utc)
        if hasattr(leaf, "not_valid_before_utc"):
            not_before = leaf.not_valid_before_utc
        else:
            not_before = leaf.not_valid_before.replace(tzinfo=timezone.utc)

        if hasattr(leaf, "not_valid_after_utc"):
            not_after = leaf.not_valid_after_utc
        else:
            not_after = leaf.not_valid_after.replace(tzinfo=timezone.utc)
        if now < not_before or now >= not_after:
            raise CertificateToolError("The issued certificate is not currently valid.")

        certificate_public_key = leaf.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if certificate_public_key != private_public_key:
            raise CertificateToolError(
                "The private key does not match the issued certificate."
            )

        return {
            "domain": domain,
            "certificate": certificate_pem.decode("utf-8"),
            "private_key": private_key_pem.decode("utf-8"),
            "expires_at": not_after.isoformat().replace("+00:00", "Z"),
        }

    def _renewal_due(self, expires_at: str) -> bool:
        expiration = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return expiration - datetime.now(timezone.utc) <= self.renew_before

    @staticmethod
    def _atomic_write(path: Path, content: bytes, mode: int):
        descriptor, temporary_path = tempfile.mkstemp(dir=path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, mode)
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.chmod(temporary_path, mode)
            os.replace(temporary_path, path)
            os.chmod(path, mode)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise

    @staticmethod
    def _check_port_80():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", 80))
        except OSError as exc:
            raise PortUnavailableError(
                "TCP port 80 is unavailable; standalone HTTP-01 cannot start."
            ) from exc
        finally:
            sock.close()

    @staticmethod
    def _run_command(command: list[str], timeout: int):
        try:
            completed = subprocess.run(
                command,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError as exc:
            raise CertificateToolError("acme.sh is not installed.") from exc
        except subprocess.TimeoutExpired as exc:
            raise CertificateTimeoutError("ACME operation timed out.") from exc

        if completed.returncode != 0:
            error = CertificateManager._sanitize_error(
                completed.stderr or completed.stdout
            )
            message = "ACME operation failed."
            if error:
                message = f"{message} {error}"
            raise CertificateToolError(message)

    @staticmethod
    def _sanitize_error(error: str) -> str:
        error = PEM_PATTERN.sub("[redacted PEM]", error or "")
        error = " ".join(error.split())
        return error[-1000:]


certificate_manager = CertificateManager()
