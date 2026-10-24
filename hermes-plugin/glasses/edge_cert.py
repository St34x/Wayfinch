# SPDX-License-Identifier: Apache-2.0
"""The device identity of a ``/ws`` upgrade (T1.184c): the client certificate the TLS edge forwards, verified here.

The plugin never trusts a forwarded fingerprint. ``EdgeCert.fingerprint(request)`` takes the one value of the header
``client_cert_header`` (absent or repeated: refused), decodes it per ``client_cert_encoding`` (``decode``: the value
is at most ``MAX_HEADER_BYTES`` before anything is decoded; ``pem-urlescaped`` is nginx's
``$ssl_client_escaped_cert``, exactly one ``CERTIFICATE`` block and nothing else; ``der-base64`` is canonical padded
standard base64) and ``verify``s it against the device CA bundle at the wall-clock time: DER (``cryptography``'s
parser is DER-strict, so no second encoding of a certificate loads; tests pin that); ``BasicConstraints`` present
with ``CA:false``; ``KeyUsage`` present with digitalSignature; EKU present and holding clientAuth; no critical
extension other than those three (RFC 5280 4.2); an EC key on P-256, P-384 or P-521 and a SHA-256/384/512
signature; ``notBefore <= now <= notAfter``; and one CA of the bundle whose subject is the cert's issuer, whose key id
(its SKI, else the RFC 5280 SHA-1 of its key) equals the cert's ``AuthorityKeyIdentifier``, which is itself valid at
now, and whose key verifies the cert's signature. Only then is the answer the SHA-256 of the DER, an exact ``str`` of
64 lower-case hex digits; every failure (any exception included) is ``None``, which the server answers with its fixed
``401``. No certificate bytes, fingerprint or exception text reach a log line or a response. Revocation is the
registry's (tombstone, deny overlay) and the edge's CRL; this module checks no CRL.

``CaBundle`` is ``signer.public_dir``/``device-ca.pem``, read with the signer-file reader of the registry (owner =
signer user, mode 0640 or stricter, regular file, no symlink, size cap): at most ``MAX_BUNDLE_BYTES`` (checked
before any pattern runs), 1 to ``MAX_CAS`` ``CERTIFICATE`` blocks, each ``CA:true`` with KeyUsage keyCertSign and the
same critical-extension, key and hash rules, nothing else. A missing or invalid bundle refuses every certificate (one
WARNING per new kind of failure, the type name only). It is checked at most once a second (``RECHECK_S``, monotonic
clock) and re-read when its inode, size, mtime, ctime, mode or owner changed, so a CA added or removed by a rotation
takes effect without a restart and a chmod is seen; nothing is kept past a failed read.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import unquote

from aiohttp import web
from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, ExtensionOID

from .config_core import GlassesConfig
from .registry_signer_file import read_signer_file
from .signer_client import public_file, signer_user_uid
from .signer_protocol import PUBLIC_CA_BUNDLE

_LOG = logging.getLogger(__name__)
_E = TypeVar("_E", bound=x509.ExtensionType)

MAX_HEADER_BYTES = 8192  # the forwarded header value, checked before any decoding
MAX_CAS = 4  # CA certificates in the bundle (two during a rotation overlap)
MAX_BUNDLE_BYTES = 64 * 1024
RECHECK_S = 1.0  # the bundle is looked at no more often than this
_PEM_CERT = re.compile(r"-----BEGIN CERTIFICATE-----\n((?:[A-Za-z0-9+/=]{1,76}\n)+)-----END CERTIFICATE-----\n?")
_BUNDLE_BLOCK = re.compile(rb"-----BEGIN CERTIFICATE-----\n(?:[A-Za-z0-9+/=]{1,76}\n)+-----END CERTIFICATE-----")
_BASE64 = re.compile(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")
_CRYPTO_ERRORS = (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm)
# The only extensions that may be critical (RFC 5280 4.2: refuse a critical extension that is not processed).
_PROCESSED = frozenset((ExtensionOID.BASIC_CONSTRAINTS, ExtensionOID.KEY_USAGE, ExtensionOID.EXTENDED_KEY_USAGE))
_CURVES = (ec.SECP256R1, ec.SECP384R1, ec.SECP521R1)  # the signer issues P-256 keys only
_HASHES = (hashes.SHA256, hashes.SHA384, hashes.SHA512)


class CertRefused(Exception):
    """The forwarded certificate or the CA bundle is not usable; carries no detail."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _canonical_base64(body: str) -> bytes:
    if not body or _BASE64.fullmatch(body) is None:
        raise CertRefused
    raw = base64.b64decode(body, validate=True)
    if base64.b64encode(raw).decode("ascii") != body:  # non-zero pad bits
        raise CertRefused
    return raw


def decode(value: str, encoding: str) -> bytes:
    """The DER bytes of the one certificate in a header *value*; ``CertRefused`` for anything else."""
    if len(value.encode("utf-8", "surrogateescape")) > MAX_HEADER_BYTES:
        raise CertRefused
    try:
        if encoding == "der-base64":
            return _canonical_base64(value)
        if encoding != "pem-urlescaped":
            raise CertRefused
        match = _PEM_CERT.fullmatch(unquote(value, encoding="ascii", errors="strict"))
        if match is None:
            raise CertRefused
        return _canonical_base64(match.group(1).replace("\n", ""))
    except (ValueError, binascii.Error):  # a bad escape or base64: the same refusal
        raise CertRefused from None


def _extension(cert: x509.Certificate, kind: type[_E]) -> _E | None:
    try:
        return cert.extensions.get_extension_for_class(kind).value
    except x509.ExtensionNotFound:
        return None


def _valid_at(cert: x509.Certificate, now: datetime) -> bool:
    return cert.not_valid_before_utc <= now <= cert.not_valid_after_utc


def _key_id(ca: x509.Certificate) -> bytes:
    ski = _extension(ca, x509.SubjectKeyIdentifier)
    return ski.digest if ski is not None else x509.SubjectKeyIdentifier.from_public_key(ca.public_key()).digest


def _issued_by(cert: x509.Certificate, ca: x509.Certificate, now: datetime) -> bool:
    aki = _extension(cert, x509.AuthorityKeyIdentifier)
    if aki is None or aki.key_identifier != _key_id(ca) or cert.issuer != ca.subject or not _valid_at(ca, now):
        return False
    try:
        cert.verify_directly_issued_by(ca)  # the signature by the CA's key
    except _CRYPTO_ERRORS:
        return False
    return True


def _profile_ok(cert: x509.Certificate, *, ca: bool) -> bool:
    """RFC 5280 and the signer's profile: ``BasicConstraints`` present with ``CA`` = *ca*; ``KeyUsage`` present with
    keyCertSign (a CA) or digitalSignature (a device); no critical extension this module does not process; an EC key
    on P-256 or a larger NIST curve and a SHA-256/384/512 signature."""
    constraints, usage = _extension(cert, x509.BasicConstraints), _extension(cert, x509.KeyUsage)
    if constraints is None or constraints.ca != ca or usage is None:
        return False
    if not (usage.key_cert_sign if ca else usage.digital_signature):
        return False
    if any(ext.critical and ext.oid not in _PROCESSED for ext in cert.extensions):
        return False
    key = cert.public_key()
    strong_key = isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, _CURVES)
    return strong_key and isinstance(cert.signature_hash_algorithm, _HASHES)


def verify(der: bytes, cas: tuple[x509.Certificate, ...], now: datetime) -> str:
    """The SHA-256 fingerprint of the device certificate *der* if one CA of *cas* vouches for it at *now*."""
    try:
        cert = x509.load_der_x509_certificate(der)  # DER-strict: no other encoding of the same certificate loads
        profile, usage = _profile_ok(cert, ca=False), _extension(cert, x509.ExtendedKeyUsage)
    except _CRYPTO_ERRORS:  # not DER, trailing data, a malformed extension, an unsupported key or hash
        raise CertRefused from None  # a repeated extension raises x509.DuplicateExtension: fingerprint() refuses it
    if not profile or usage is None or ExtendedKeyUsageOID.CLIENT_AUTH not in usage:
        raise CertRefused
    if not _valid_at(cert, now) or not any(_issued_by(cert, ca, now) for ca in cas):
        raise CertRefused
    return hashlib.sha256(der).hexdigest()


def parse_bundle(raw: bytes) -> tuple[x509.Certificate, ...]:
    """The CA certificates of a bundle: 1 to ``MAX_CAS`` PEM blocks, each a CA by ``_profile_ok``, only whitespace
    around them; the size is checked before any pattern runs."""
    if len(raw) > MAX_BUNDLE_BYTES:
        raise CertRefused
    blocks = _BUNDLE_BLOCK.findall(raw)
    if not 1 <= len(blocks) <= MAX_CAS or _BUNDLE_BLOCK.sub(b"", raw).strip():
        raise CertRefused
    cas = tuple(x509.load_pem_x509_certificate(block) for block in blocks)
    if not all(_profile_ok(ca, ca=True) for ca in cas):
        raise CertRefused
    return cas


class CaBundle:
    """``device-ca.pem`` in the signer's *directory*, owned by *signer_uid*; see the module docstring."""

    def __init__(self, directory: Path, signer_uid: int, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._dir, self._uid, self._monotonic = directory, signer_uid, monotonic
        self._checked: float | None = None
        self._seen: tuple[int, ...] | None = None
        self._cas: tuple[x509.Certificate, ...] | None = None
        self._failure: str | None = None

    def _stamp(self) -> tuple[int, ...]:
        st = os.stat(self._dir / PUBLIC_CA_BUNDLE, follow_symlinks=False)
        return st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_mode, st.st_uid

    def cas(self) -> tuple[x509.Certificate, ...]:
        """The CAs of the bundle as last read; ``CertRefused`` while it is missing or invalid."""
        now = self._monotonic()
        if self._checked is None or now - self._checked >= RECHECK_S:
            self._checked = now
            self._refresh()
        if self._cas is None:
            raise CertRefused
        return self._cas

    def _refresh(self) -> None:
        try:
            stamp = self._stamp()
            if self._cas is None or stamp != self._seen:
                self._cas = None
                self._cas, self._seen = parse_bundle(read_signer_file(self._dir, self._uid, PUBLIC_CA_BUNDLE)), stamp
        except Exception as exc:  # noqa: BLE001 - any failure refuses every certificate; the type name only
            self._cas = None
            if self._failure != type(exc).__name__:
                _LOG.warning(
                    "glasses ws: device CA bundle unusable (%s); refusing every certificate", type(exc).__name__
                )
            self._failure = type(exc).__name__
            return
        self._failure = None


class EdgeCert:
    """The verified fingerprint of an upgrade's forwarded certificate, or ``None`` (the request is refused)."""

    def __init__(
        self,
        header: str,
        encoding: str,
        cas: Callable[[], tuple[x509.Certificate, ...]],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._header, self._encoding, self._cas, self._clock = header, encoding, cas, clock

    def fingerprint(self, request: web.BaseRequest) -> str | None:
        values = request.headers.getall(self._header, [])
        if len(values) != 1:
            return None
        try:
            return verify(decode(values[0], self._encoding), self._cas(), self._clock())
        except Exception:  # noqa: BLE001 - every decode, parse or verification failure is the same refusal
            _LOG.debug("glasses ws: client certificate refused")
            return None


def edge_cert(cfg: GlassesConfig) -> EdgeCert:
    """The checker for ``client_cert_header`` / ``client_cert_encoding`` over the signer's CA bundle."""
    bundle = CaBundle(public_file(cfg, PUBLIC_CA_BUNDLE).parent, signer_user_uid(cfg))
    return EdgeCert(cfg.client_cert_header, cfg.client_cert_encoding, bundle.cas)
