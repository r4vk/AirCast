"""Cast device authentication providers.

A Cast sender opens TLS to the receiver and sends a DeviceAuthMessage challenge. The
receiver answers with a device certificate chain and a signature over the TLS peer
certificate (prefixed with the sender nonce, when one is sent). Official senders (Android,
Chrome) only accept chains rooted in Google's Cast root CA.

AirCast ships NO Google-signed material. Two providers exist:

* ``selfsigned`` (default): generates its own CA, device and TLS certificates. Works with
  open-source senders that skip verification (pychromecast, catt, VLC, many third-party
  apps); stock Android / Chrome senders will refuse it.
* ``files``: loads certificates, keys and/or precomputed signatures you provide from a
  directory (see docs/CAST_AUTH.md). You are responsible for having the right to use them.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import ssl
from abc import ABC, abstractmethod
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from aircast.cast.proto import (
    HASH_SHA256,
    SIG_RSASSA_PSS,
    AuthChallenge,
    AuthResponse,
)

_LOGGER = logging.getLogger(__name__)


class AuthError(Exception):
    pass


class AuthProvider(ABC):
    tls_cert_path: Path
    tls_key_path: Path

    def ssl_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.tls_cert_path), str(self.tls_key_path))
        return context

    @property
    def peer_cert_der(self) -> bytes:
        cert = x509.load_pem_x509_certificate(self.tls_cert_path.read_bytes())
        return cert.public_bytes(serialization.Encoding.DER)

    @abstractmethod
    def respond(self, challenge: AuthChallenge) -> AuthResponse:
        """Build the AuthResponse for a challenge."""


def _sign(key: rsa.RSAPrivateKey, data: bytes, challenge: AuthChallenge) -> bytes:
    algorithm = hashes.SHA256() if challenge.hash_algorithm == HASH_SHA256 else hashes.SHA1()
    if challenge.signature_algorithm == SIG_RSASSA_PSS:
        pad = padding.PSS(mgf=padding.MGF1(algorithm), salt_length=32)
    else:
        pad = padding.PKCS1v15()
    return key.sign(data, pad, algorithm)


def _pem_certs_to_der(data: bytes) -> list[bytes]:
    return [
        cert.public_bytes(serialization.Encoding.DER)
        for cert in x509.load_pem_x509_certificates(data)
    ]


# -- selfsigned ---------------------------------------------------------------------


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _new_cert(
    subject_cn: str,
    key: rsa.RSAPrivateKey,
    issuer_cn: str,
    issuer_key: rsa.RSAPrivateKey,
    *,
    ca: bool,
    days: int,
) -> x509.Certificate:
    now = dt.datetime.now(dt.UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(issuer_key, hashes.SHA256())
    )


def _write_pem(path: Path, obj) -> None:
    if isinstance(obj, rsa.RSAPrivateKey):
        data = obj.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    else:
        data = obj.public_bytes(serialization.Encoding.PEM)
    path.write_bytes(data)
    if isinstance(obj, rsa.RSAPrivateKey):
        path.chmod(0o600)


class SelfSignedProvider(AuthProvider):
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.tls_cert_path = directory / "tls.crt"
        self.tls_key_path = directory / "tls.key"
        device_cert_path = directory / "device.crt"
        device_key_path = directory / "device.key"
        ca_cert_path = directory / "ca.crt"

        if not all(p.exists() for p in (self.tls_cert_path, self.tls_key_path,
                                         device_cert_path, device_key_path, ca_cert_path)):
            _LOGGER.info("Generating self-signed Cast certificates in %s", directory)
            ca_key = _new_key()
            ca_cert = _new_cert("AirCast Local CA", ca_key, "AirCast Local CA", ca_key,
                                ca=True, days=3650)
            device_key = _new_key()
            device_cert = _new_cert("AirCast Device", device_key, "AirCast Local CA", ca_key,
                                    ca=False, days=3650)
            tls_key = _new_key()
            tls_cert = _new_cert("AirCast", tls_key, "AirCast", tls_key, ca=False, days=3650)
            _write_pem(ca_cert_path, ca_cert)
            _write_pem(device_cert_path, device_cert)
            _write_pem(device_key_path, device_key)
            _write_pem(self.tls_cert_path, tls_cert)
            _write_pem(self.tls_key_path, tls_key)

        self._device_key = serialization.load_pem_private_key(
            device_key_path.read_bytes(), password=None
        )
        self._device_der = _pem_certs_to_der(device_cert_path.read_bytes())[0]
        self._chain = _pem_certs_to_der(ca_cert_path.read_bytes())

    def respond(self, challenge: AuthChallenge) -> AuthResponse:
        data = (challenge.sender_nonce or b"") + self.peer_cert_der
        return AuthResponse(
            signature=_sign(self._device_key, data, challenge),  # type: ignore[arg-type]
            client_auth_certificate=self._device_der,
            intermediate_certificates=self._chain,
            signature_algorithm=challenge.signature_algorithm,
            sender_nonce=challenge.sender_nonce,
            hash_algorithm=challenge.hash_algorithm,
        )


# -- files --------------------------------------------------------------------------


class FilesProvider(AuthProvider):
    """User-supplied material. Layout (all PEM unless noted):

    tls.crt, tls.key          TLS server certificate/key presented to senders (required)
    device.crt                device certificate (required)
    intermediates.pem         intermediate chain, one or more certificates (optional)
    device.key                device private key: signatures are computed live, or
    signatures.json           precomputed signatures over tls.crt, chosen by date:
                              {"signatures": [{"not_before": ISO-8601, "not_after": ISO-8601,
                                               "signature": base64}]}
    crl.bin                   binary CRL bundle forwarded to senders (optional)
    """

    def __init__(self, directory: Path) -> None:
        if not directory.is_dir():
            raise AuthError(f"cast_auth_dir {directory} does not exist")
        self.tls_cert_path = directory / "tls.crt"
        self.tls_key_path = directory / "tls.key"
        for required in (self.tls_cert_path, self.tls_key_path, directory / "device.crt"):
            if not required.exists():
                raise AuthError(f"missing {required}")

        self._device_der = _pem_certs_to_der((directory / "device.crt").read_bytes())[0]
        chain = directory / "intermediates.pem"
        self._chain = _pem_certs_to_der(chain.read_bytes()) if chain.exists() else []
        crl = directory / "crl.bin"
        self._crl = crl.read_bytes() if crl.exists() else None

        key_path = directory / "device.key"
        sig_path = directory / "signatures.json"
        self._device_key = None
        self._signatures: list[tuple[dt.datetime, dt.datetime, bytes]] = []
        if key_path.exists():
            self._device_key = serialization.load_pem_private_key(
                key_path.read_bytes(), password=None
            )
        elif sig_path.exists():
            raw = json.loads(sig_path.read_text(encoding="utf-8"))
            for item in raw.get("signatures", []):
                self._signatures.append(
                    (_parse_dt(item["not_before"]), _parse_dt(item["not_after"]),
                     base64.b64decode(item["signature"]))
                )
            if not self._signatures:
                raise AuthError(f"{sig_path} contains no signatures")
        else:
            raise AuthError(f"{directory} needs device.key or signatures.json")

    def _precomputed(self) -> bytes:
        now = dt.datetime.now(dt.UTC)
        for not_before, not_after, signature in self._signatures:
            if not_before <= now < not_after:
                return signature
        raise AuthError("no precomputed signature is valid for the current date")

    def respond(self, challenge: AuthChallenge) -> AuthResponse:
        if self._device_key is not None:
            data = (challenge.sender_nonce or b"") + self.peer_cert_der
            signature = _sign(self._device_key, data, challenge)  # type: ignore[arg-type]
            nonce = challenge.sender_nonce
        else:
            signature = self._precomputed()
            nonce = None
        return AuthResponse(
            signature=signature,
            client_auth_certificate=self._device_der,
            intermediate_certificates=self._chain,
            signature_algorithm=challenge.signature_algorithm,
            sender_nonce=nonce,
            hash_algorithm=challenge.hash_algorithm,
            crl=self._crl,
        )


def _parse_dt(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def load_auth_provider(kind: str, state_dir: str, auth_dir: str | None) -> AuthProvider:
    if kind == "files":
        if not auth_dir:
            raise AuthError("cast_auth=files requires cast_auth_dir")
        return FilesProvider(Path(auth_dir))
    if kind == "selfsigned":
        return SelfSignedProvider(Path(state_dir) / "cast")
    raise AuthError(f"unknown cast_auth provider: {kind}")
