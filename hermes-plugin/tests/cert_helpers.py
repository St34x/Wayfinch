# SPDX-License-Identifier: Apache-2.0
"""Test certificates for the forwarded-certificate check: device CAs and device certificates built with
``cryptography`` in the profile the device-CA signer issues (CA: self-signed, ``CA:true pathlen=0``, key usage
keyCertSign + cRLSign, SKI; device: ``CA:false``, digitalSignature, EKU clientAuth, SKI, AKI, 90 days), plus the
two header encodings an edge forwards (nginx ``$ssl_client_escaped_cert``, Caddy's base64 DER)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NOW = datetime.now(UTC).replace(microsecond=0)  # wall clock: the adapter tests verify with the real clock
HEADER = "X-Client-Cert"


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@dataclass(frozen=True)
class Ca:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate

    @property
    def pem(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.PEM)

    @property
    def ski(self) -> bytes:
        return x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()).digest


def key_usage(*bits: str) -> x509.KeyUsage:
    """A KeyUsage with exactly *bits* (attribute names such as ``digital_signature``) set."""
    names = ("digital_signature", "content_commitment", "key_encipherment", "data_encipherment", "key_agreement")
    names += ("key_cert_sign", "crl_sign", "encipher_only", "decipher_only")
    return x509.KeyUsage(**{name: name in bits for name in names})


CA_USAGE = key_usage("key_cert_sign", "crl_sign")
DEVICE_USAGE = key_usage("digital_signature")
Extra = tuple[tuple[x509.ExtensionType, bool], ...]  # (extension, critical) pairs added to a profile


def _added(builder: x509.CertificateBuilder, usage: x509.KeyUsage | None, extra: Extra) -> x509.CertificateBuilder:
    if usage is not None:  # None: the KeyUsage extension is left out
        builder = builder.add_extension(usage, critical=True)
    for extension, critical in extra:
        builder = builder.add_extension(extension, critical=critical)
    return builder


def make_ca(
    cn: str = "glasses device CA 0001",
    *,
    days: int = 3650,
    key=None,
    usage: x509.KeyUsage | None = CA_USAGE,
    extra: Extra = (),
    digest: hashes.HashAlgorithm | None = None,
) -> Ca:
    """A device CA in the signer's profile; *usage* replaces its KeyUsage (None: none), *extra* is added, *digest*
    is the self-signature hash (SHA-256 by default)."""
    key = key or _key()
    builder = (
        x509
        .CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    return Ca(key, _added(builder, usage, extra).sign(key, digest or hashes.SHA256()))


def device_cert(
    ca: Ca,
    *,
    start: datetime | None = None,
    days: int = 90,
    is_ca: bool = False,
    eku: tuple[x509.ObjectIdentifier, ...] = (ExtendedKeyUsageOID.CLIENT_AUTH,),
    aki: bytes | None = None,
    issuer: x509.Name | None = None,
    signer: ec.EllipticCurvePrivateKey | None = None,
    basic_constraints: bool = True,
    with_aki: bool = True,
    usage: x509.KeyUsage | None = DEVICE_USAGE,
    extra: Extra = (),
    key: ec.EllipticCurvePrivateKey | None = None,
    digest: hashes.HashAlgorithm | None = None,
) -> x509.Certificate:
    """A device certificate issued by *ca*; each keyword changes one field away from the signer's profile
    (``basic_constraints`` / ``with_aki`` False, ``usage`` None: that extension is left out; ``key``: the device's
    key, ``digest``: the signature hash, SHA-256 by default)."""
    key = key or _key()
    begin = start or NOW - timedelta(days=1)
    builder = (
        x509
        .CertificateBuilder()
        .subject_name(_name("glasses device"))
        .issuer_name(issuer or ca.cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(begin)
        .not_valid_after(begin + timedelta(days=days))
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if basic_constraints:
        builder = builder.add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
    if with_aki:
        builder = builder.add_extension(x509.AuthorityKeyIdentifier(aki or ca.ski, None, None), critical=False)
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=False)
    return _added(builder, usage, extra).sign(signer or ca.key, digest or hashes.SHA256())


def self_signed() -> x509.Certificate:
    """A device-profile certificate signed by its own key (issuer = subject, AKI = its own SKI)."""
    key = _key()
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    builder = (
        x509
        .CertificateBuilder()
        .subject_name(_name("glasses device"))
        .issuer_name(_name("glasses device"))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=89))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(ski, critical=False)
        .add_extension(x509.AuthorityKeyIdentifier(ski.digest, None, None), critical=False)
    )
    return builder.sign(key, hashes.SHA256())


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(der(cert)).hexdigest()


def pem_escaped(cert: x509.Certificate) -> str:
    """The value nginx puts in ``$ssl_client_escaped_cert``: the PEM, URL-escaped."""
    return quote(cert.public_bytes(serialization.Encoding.PEM).decode("ascii"), safe="")


def der_base64(cert: x509.Certificate) -> str:
    return base64.b64encode(der(cert)).decode("ascii")


def encoded(cert: x509.Certificate, encoding: str) -> str:
    return pem_escaped(cert) if encoding == "pem-urlescaped" else der_base64(cert)
