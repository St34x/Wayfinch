# SPDX-License-Identifier: Apache-2.0
"""The forwarded certificate's chain checks (``edge_cert.verify`` and ``EdgeCert.fingerprint``): a cert of either CA
of a two-CA bundle passes and yields the SHA-256 of its DER as an exact ``str``; SAT-SC-12 (another CA, self-signed,
expired, not yet valid, ``CA:true``, EKU without clientAuth) and the adversarial cases (a forged issuer name with the
right AKI, a mismatched AKI, an expired CA) are refused. Each check has a case that only it refuses (mutants)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from cert_helpers import NOW, Ca, der, device_cert, encoded, fingerprint, key_usage, make_ca, self_signed
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
from multidict import CIMultiDict

from glasses import edge_cert
from glasses.edge_cert import CertRefused, EdgeCert

CA_A = make_ca("glasses device CA aaaa")
CA_B = make_ca("glasses device CA bbbb")
BUNDLE = (CA_A.cert, CA_B.cert)
_UNKNOWN = x509.UnrecognizedExtension(x509.ObjectIdentifier("1.3.6.1.4.1.55555.1"), b"\x05\x00")
_ANY_POLICY = x509.CertificatePolicies([x509.PolicyInformation(x509.ObjectIdentifier("2.5.29.32.0"), None)])


class _Request:
    def __init__(self, headers: list[tuple[str, str]]) -> None:
        self.headers = CIMultiDict(headers)


def _verify(cert: x509.Certificate, cas: tuple[x509.Certificate, ...] = BUNDLE, now=NOW) -> str:
    return edge_cert.verify(der(cert), cas, now)


@pytest.mark.parametrize("ca", [CA_A, CA_B], ids=["first-ca", "second-ca"])
@pytest.mark.parametrize("encoding", ["pem-urlescaped", "der-base64"])
def test_a_cert_of_either_ca_passes_with_the_sha256_of_its_der(ca: Ca, encoding: str):
    cert = device_cert(ca)
    checker = EdgeCert("X-Client-Cert", encoding, lambda: BUNDLE, clock=lambda: NOW)
    fp = checker.fingerprint(_Request([("X-Client-Cert", encoded(cert, encoding))]))
    assert fp == fingerprint(cert) and type(fp) is str


def _foreign() -> x509.Certificate:
    return device_cert(make_ca("glasses device CA cccc"))


def _forged_name() -> x509.Certificate:
    """Claims CA A as issuer and carries CA A's key id, but is signed by another key: only the signature check."""
    return device_cert(CA_A, signer=ec.generate_private_key(ec.SECP256R1()))


REFUSED = {
    "another-ca": _foreign,
    "self-signed": self_signed,
    "expired": lambda: device_cert(CA_A, start=NOW - timedelta(days=91)),
    "not-yet-valid": lambda: device_cert(CA_A, start=NOW + timedelta(seconds=1)),
    "ca-true": lambda: device_cert(CA_A, is_ca=True),
    "no-basic-constraints": lambda: device_cert(CA_A, basic_constraints=False),
    "aki-absent": lambda: device_cert(CA_A, with_aki=False),
    "eku-without-client-auth": lambda: device_cert(CA_A, eku=(ExtendedKeyUsageOID.SERVER_AUTH,)),
    "no-eku": lambda: device_cert(CA_A, eku=()),
    "forged-issuer-name": _forged_name,
    "mismatched-aki": lambda: device_cert(CA_A, aki=b"\x01" * 20),
    "issuer-name-of-the-other-ca": lambda: device_cert(CA_A, issuer=CA_B.cert.subject),
    "unknown-critical-extension": lambda: device_cert(CA_A, extra=((_UNKNOWN, True),)),
    "critical-extension-not-processed": lambda: device_cert(CA_A, extra=((_ANY_POLICY, True),)),
    "no-key-usage": lambda: device_cert(CA_A, usage=None),
    "key-usage-without-digital-signature": lambda: device_cert(CA_A, usage=key_usage("key_agreement")),
    "key-on-secp256k1": lambda: device_cert(CA_A, key=ec.generate_private_key(ec.SECP256K1())),
    "sha224-signature": lambda: device_cert(CA_A, digest=hashes.SHA224()),
}


@pytest.mark.parametrize("make", REFUSED.values(), ids=REFUSED.keys())
def test_sat_sc_12_and_adversarial_certs_are_refused(make):
    with pytest.raises(CertRefused):
        _verify(make())


@pytest.mark.parametrize(
    "make",
    [
        lambda: device_cert(CA_A, extra=((_UNKNOWN, False),)),
        lambda: device_cert(CA_A, key=ec.generate_private_key(ec.SECP384R1()), digest=hashes.SHA384()),
    ],
    ids=["unknown-non-critical-extension", "p384-key-sha384-signature"],
)
def test_a_non_critical_extension_and_a_stronger_key_are_accepted(make):
    cert = make()
    assert _verify(cert) == fingerprint(cert)


def test_validity_bounds_are_inclusive_and_the_clock_is_the_injected_one():
    cert = device_cert(CA_A, start=NOW, days=90)
    assert _verify(cert, now=NOW) == fingerprint(cert)
    assert _verify(cert, now=NOW + timedelta(days=90)) == fingerprint(cert)
    with pytest.raises(CertRefused):
        _verify(cert, now=NOW + timedelta(days=90, seconds=1))


def test_a_ca_outside_its_validity_issues_nothing():
    old = make_ca("glasses device CA dddd", days=0)  # valid from NOW - 1 day until NOW
    cert = device_cert(old)
    with pytest.raises(CertRefused):
        _verify(cert, (old.cert,), NOW + timedelta(seconds=1))


def test_the_ca_with_the_same_name_but_another_key_does_not_vouch():
    twin = make_ca("glasses device CA aaaa")  # CA A's name, its own key
    with pytest.raises(CertRefused):
        _verify(device_cert(CA_A), (twin.cert,))
    assert _verify(device_cert(twin), (CA_B.cert, twin.cert))


def test_an_empty_bundle_refuses_every_cert():
    with pytest.raises(CertRefused):
        _verify(device_cert(CA_A), ())


def _tlv(tag: int, body: bytes) -> bytes:
    size = len(body)
    octets = (size.bit_length() + 7) // 8
    length = bytes([size]) if size < 0x80 else bytes([0x80 | octets]) + size.to_bytes(octets, "big")
    return bytes([tag]) + length + body


def _resigned(tbs: bytes, cert: x509.Certificate) -> bytes:
    """A certificate of *tbs* with CA A's signature algorithm (taken from *cert*) and a valid signature over *tbs*."""
    raw, own = der(cert), cert.tbs_certificate_bytes
    assert raw[1] == 0x82 and raw[4 : 4 + len(own)] == own  # 4-byte outer header, then the TBS
    alg_at = 4 + len(own)
    alg = raw[alg_at : alg_at + 2 + raw[alg_at + 1]]
    return _tlv(0x30, tbs + alg + _tlv(0x03, b"\x00" + CA_A.key.sign(tbs, ec.ECDSA(hashes.SHA256()))))


_BC_CRITICAL = b"\x06\x03\x55\x1d\x13\x01\x01\xff"  # basicConstraints, critical TRUE (DER: 0xFF)


def _non_canonical() -> dict[str, bytes]:
    cert = device_cert(CA_A)
    raw, tbs = der(cert), cert.tbs_certificate_bytes
    assert tbs.count(_BC_CRITICAL) == 1 and edge_cert.verify(_resigned(tbs, cert), BUNDLE, NOW)  # the rebuild is valid
    sig_at = 4 + len(tbs) + 2 + raw[4 + len(tbs) + 1]
    return {
        "trailing-data": raw + b"\x00",
        "outer-length-not-minimal": b"\x30\x83\x00" + raw[2:],  # the signed bytes are unchanged
        "signature-length-not-minimal": _tlv(0x30, raw[4:sig_at] + b"\x03\x81" + raw[sig_at + 1 :]),
        "boolean-true-as-01": _resigned(tbs.replace(_BC_CRITICAL, _BC_CRITICAL[:-1] + b"\x01"), cert),
    }


@pytest.mark.parametrize(
    "kind", ["trailing-data", "outer-length-not-minimal", "signature-length-not-minimal", "boolean-true-as-01"]
)
def test_a_non_canonical_der_with_a_valid_signature_is_refused(kind: str):
    """The DER parser itself refuses these, so ``verify`` has no re-encoding check of its own; a looser parser in a
    later ``cryptography`` (one certificate, two fingerprints) fails here."""
    with pytest.raises(CertRefused):
        edge_cert.verify(_non_canonical()[kind], BUNDLE, NOW)


def test_the_header_fingerprint_of_another_device_is_ignored():
    mine, other = device_cert(CA_A), device_cert(CA_B)
    checker = EdgeCert("X-Client-Cert", "der-base64", lambda: BUNDLE, clock=lambda: NOW)
    headers = [("X-Client-Cert", encoded(mine, "der-base64")), ("X-Client-Cert-Fp", fingerprint(other))]
    assert checker.fingerprint(_Request(headers)) == fingerprint(mine)


@pytest.mark.parametrize(
    "headers",
    [
        [("X-Client-Cert-Fp", "ab" * 32)],  # the old fingerprint header alone
        [],
        [("X-Client-Cert", "")],
    ],
    ids=["fingerprint-header-only", "no-header", "empty-header"],
)
def test_no_certificate_header_is_a_refusal(headers):
    checker = EdgeCert("X-Client-Cert", "der-base64", lambda: BUNDLE, clock=lambda: NOW)
    assert checker.fingerprint(_Request(headers)) is None


def test_the_certificate_header_twice_is_refused_even_when_both_are_valid():
    value = encoded(device_cert(CA_A), "der-base64")
    checker = EdgeCert("X-Client-Cert", "der-base64", lambda: BUNDLE, clock=lambda: NOW)
    assert checker.fingerprint(_Request([("X-Client-Cert", value), ("x-client-cert", value)])) is None


def test_a_failing_bundle_reader_or_clock_is_a_refusal_not_an_exception():
    value = encoded(device_cert(CA_A), "der-base64")

    def broken() -> tuple[x509.Certificate, ...]:
        raise OSError("bundle")

    assert (
        EdgeCert("X-Client-Cert", "der-base64", broken, clock=lambda: NOW).fingerprint(
            _Request([("X-Client-Cert", value)])
        )
        is None
    )
    assert (
        EdgeCert("X-Client-Cert", "der-base64", lambda: BUNDLE, clock=lambda: 1 / 0).fingerprint(
            _Request([("X-Client-Cert", value)])
        )
        is None
    )


def test_an_unknown_encoding_refuses_every_cert():
    value = encoded(device_cert(CA_A), "der-base64")
    checker = EdgeCert("X-Client-Cert", "der", lambda: BUNDLE, clock=lambda: NOW)
    assert checker.fingerprint(_Request([("X-Client-Cert", value)])) is None
