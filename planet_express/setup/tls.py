"""A self-signed certificate for the setup server, generated in memory at start.

The browser will warn once; the terminal prints the certificate's SHA-256 fingerprint so a careful operator
can compare it. The private key never touches disk except in a 0700 temporary directory that `serve` removes.

This module never imports `config`: setup runs before any config exists.
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
from dataclasses import dataclass


@dataclass(frozen=True)
class Certificate:
    cert_pem: bytes
    key_pem: bytes
    fingerprint: str          # SHA-256 of the DER certificate, colon-separated upper-case hex


def make_certificate(names: list[str], addresses: list[str], *, days: int = 2) -> Certificate:
    """A short-lived ECDSA P-256 certificate valid for these names and IP addresses."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Planet Express setup")])
    alt = [x509.DNSName(n) for n in dict.fromkeys(names)]
    alt += [x509.IPAddress(ipaddress.ip_address(a)) for a in dict.fromkeys(addresses)]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return Certificate(
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)))
