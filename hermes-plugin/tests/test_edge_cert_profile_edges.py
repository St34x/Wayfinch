# SPDX-License-Identifier: Apache-2.0
"""Edges of the certificate profile and the header and bundle limits of ``edge_cert`` that the main cases leave open.

Device certificate: no ``BasicConstraints`` at all, no ``AuthorityKeyIdentifier``, ``anyExtendedKeyUsage`` instead of
clientAuth (each refused; the same builder with the field present passes), clientAuth next to serverAuth (passes).
CA key id: a CA without a ``SubjectKeyIdentifier`` is matched by the RFC 5280 hash of its key, a CA that carries a
different SKI only by that SKI. Header: the 8 KiB cap counts bytes, not characters; odd characters (surrogate-escaped
bytes, NUL, a right-to-left mark, non-ASCII) are a refusal, never an exception. Bundle: exactly four CAs pass, one
device certificate among CAs refuses the whole bundle, an oversize file is refused.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from cert_helpers import CA_USAGE, NOW, Ca, der, device_cert, encoded, fingerprint, make_ca, pem_escaped
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from multidict import CIMultiDict

from glasses import edge_cert
from glasses.edge_cert import MAX_CAS, MAX_HEADER_BYTES, CertRefused, EdgeCert, parse_bundle

CA = make_ca("glasses device CA aaaa")
CLIENT = ExtendedKeyUsageOID.CLIENT_AUTH


class _Request:
    def __init__(self, value: str) -> None:
        self.headers = CIMultiDict([("X-Client-Cert", value)])


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _issue(
    ca: Ca,
    *,
    basic_constraints: bool = True,
    aki: bytes | None = None,
    with_aki: bool = True,
    eku: tuple[x509.ObjectIdentifier, ...] = (CLIENT,),
) -> x509.Certificate:
    """A device certificate of *ca* in the signer's profile (digitalSignature), each extension switchable."""
    return device_cert(ca, basic_constraints=basic_constraints, aki=aki, with_aki=with_aki, eku=eku)


def _ca(*, ski: bytes | None, cn: str = "glasses device CA ssss") -> Ca:
    """A device-CA profile certificate (keyCertSign) with the given SKI value, or with no SKI extension (``None``)."""
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        x509
        .CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(_name(cn))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(CA_USAGE, critical=True)
    )
    if ski is not None:
        builder = builder.add_extension(x509.SubjectKeyIdentifier(ski), critical=False)
    return Ca(key, builder.sign(key, hashes.SHA256()))


def _verify(cert: x509.Certificate, *cas: Ca) -> str:
    return edge_cert.verify(der(cert), tuple(ca.cert for ca in cas) or (CA.cert,), NOW)


def _fingerprint_of(value: str, encoding: str) -> str | None:
    return EdgeCert("X-Client-Cert", encoding, lambda: (CA.cert,), clock=lambda: NOW).fingerprint(_Request(value))


# --- the device certificate ---------------------------------------------------------------------------------------


def test_the_builder_with_every_field_present_is_accepted():
    cert = _issue(CA)
    assert _verify(cert) == fingerprint(cert)


def test_a_cert_without_basic_constraints_is_refused():
    with pytest.raises(CertRefused):
        _verify(_issue(CA, basic_constraints=False))


def test_a_cert_without_an_authority_key_identifier_is_refused():
    with pytest.raises(CertRefused):
        _verify(_issue(CA, with_aki=False))


def test_client_auth_next_to_server_auth_passes():
    cert = _issue(CA, eku=(ExtendedKeyUsageOID.SERVER_AUTH, CLIENT))
    assert _verify(cert) == fingerprint(cert)


def test_any_extended_key_usage_is_not_client_auth():
    with pytest.raises(CertRefused):
        _verify(_issue(CA, eku=(ExtendedKeyUsageOID.ANY_EXTENDED_KEY_USAGE,)))


# --- how the CA's key id is found ----------------------------------------------------------------------------------


def test_a_ca_without_a_subject_key_identifier_is_matched_by_the_rfc_5280_hash_of_its_key():
    ca = _ca(ski=None)
    good = _issue(ca)  # the AKI the builder derives from the CA key: the RFC 5280 SHA-1 method
    assert _verify(good, ca) == fingerprint(good)
    with pytest.raises(CertRefused):
        _verify(_issue(ca, aki=b"\x05" * 20), ca)


def test_a_ca_with_its_own_subject_key_identifier_is_matched_by_that_value_only():
    ca = _ca(ski=b"\x07" * 20)
    assert _verify(_issue(ca, aki=b"\x07" * 20), ca)
    with pytest.raises(CertRefused):  # the hash of the key is not what this CA calls itself
        _verify(_issue(ca), ca)


# --- the header value ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["pem-urlescaped", "der-base64"])
def test_the_length_cap_counts_bytes_not_characters(monkeypatch, encoding):
    reached: list[str] = []
    monkeypatch.setattr(edge_cert, "unquote", lambda *a, **k: reached.append("unquote") or "")
    monkeypatch.setattr(edge_cert, "_canonical_base64", lambda *a, **k: reached.append("base64") or b"")
    value = "é" * (MAX_HEADER_BYTES // 2 + 1)  # 4097 characters, 8194 bytes
    assert len(value) <= MAX_HEADER_BYTES
    with pytest.raises(CertRefused):
        edge_cert.decode(value, encoding)
    assert reached == []


ODD = {
    "surrogate-escaped-byte": "\udcff",  # what aiohttp makes of a header byte that is not UTF-8
    "nul": "\x00",
    "right-to-left-mark": "\u202e",
    "non-ascii": "é",
    "tab-inside": "AAAA\tAAAA",
    "lone-percent": "%",
}


@pytest.mark.parametrize("encoding", ["pem-urlescaped", "der-base64"])
@pytest.mark.parametrize("value", ODD.values(), ids=ODD.keys())
def test_odd_characters_in_the_header_are_a_refusal_never_an_exception(value, encoding):
    assert _fingerprint_of(value, encoding) is None
    assert _fingerprint_of(encoded(_issue(CA), encoding) + value, encoding) is None


@pytest.mark.parametrize("encoding", ["pem-urlescaped", "der-base64"])
def test_the_good_value_is_accepted_by_the_same_checker(encoding):
    cert = _issue(CA)
    assert _fingerprint_of(encoded(cert, encoding), encoding) == fingerprint(cert)


def test_percent_escapes_in_lower_case_are_the_same_certificate():
    cert = _issue(CA)
    lowered = re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), pem_escaped(cert))
    assert lowered != pem_escaped(cert)
    assert _fingerprint_of(lowered, "pem-urlescaped") == fingerprint(cert)


# --- the bundle ----------------------------------------------------------------------------------------------------


def test_a_bundle_of_exactly_the_maximum_number_of_cas_is_accepted():
    cas = [make_ca(f"glasses device CA {n:04d}") for n in range(MAX_CAS)]
    assert len(parse_bundle(b"".join(ca.pem for ca in cas))) == MAX_CAS


def test_one_device_certificate_among_cas_refuses_the_whole_bundle():
    device = _issue(CA).public_bytes(serialization.Encoding.PEM)
    with pytest.raises(CertRefused):
        parse_bundle(CA.pem + device + make_ca("glasses device CA bbbb").pem)


def test_an_oversize_bundle_is_refused():
    with pytest.raises(CertRefused):
        parse_bundle(CA.pem + b"\n" * (edge_cert.MAX_BUNDLE_BYTES + 1))
