# SPDX-License-Identifier: Apache-2.0
"""Edges of the published-bundle check (``crl_check``): bundle shapes (count, non-CA, junk, size), CRLs that verify
but carry the wrong fields, the clock limits, and the ``crl-seen.json`` record (malformed, unsafe, partial raise)."""

from __future__ import annotations

import base64
import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from signer_bundles import DAY, NOW, crl_builder, make_ca, make_crl, make_site, pem, place

from glasses import crl_check
from glasses import signer_protocol as proto


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def check(site, when=NOW) -> crl_check.CrlCheck:
    real = crl_check._utcnow
    try:
        crl_check._utcnow = lambda: when
        public = site.public
        return crl_check.check_published(public / proto.PUBLIC_CA_BUNDLE, public / proto.PUBLIC_CRL, site.state)
    finally:
        crl_check._utcnow = real


def only_error(result: crl_check.CrlCheck) -> str:
    assert len(result.errors) == 1, result
    return result.errors[0]


def test_three_cas_or_a_non_ca_cert_in_the_bundle_is_an_error(site):
    cas = [make_ca(), make_ca(), make_ca()]
    place(site.public, cas, [make_crl(ca) for ca in cas])
    assert "holds 3 certificates, 1 or 2 expected" in only_error(check(site))
    leaf = make_ca(is_ca=False)
    place(site.public, [leaf], [make_crl(leaf)])
    assert "is not a CA" in only_error(check(site))


def test_text_around_the_crls_or_an_oversized_file_is_an_error(site):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca) + b"trailing text\n"])
    assert only_error(check(site)) == "devices-crl.pem: not a valid PEM CRL bundle"
    (site.public / proto.PUBLIC_CRL).write_bytes(b"\n" * (crl_check.PUBLISHED_CAP + 1))
    assert "larger than" in only_error(check(site))


def test_a_crl_that_verifies_under_both_bundle_cas_is_an_error(site):
    first = make_ca()
    twin = make_ca(key=first.key)  # same subject and key, another cert
    place(site.public, [first, twin], [make_crl(first)])
    assert "not signed by exactly one CA" in check(site).errors[0]
    assert not site.record.exists()


def test_two_crls_for_one_ca_is_an_error_and_nothing_is_recorded(site):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, number=1), make_crl(ca, number=2)])
    assert "2 CRLs in devices-crl.pem, exactly 1 expected" in only_error(check(site))
    assert not site.record.exists()


@pytest.mark.parametrize("issuer", [x509.Name([]), x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "another CA")])])
def test_a_crl_signed_by_the_ca_key_under_another_issuer_name_is_an_error(site, issuer):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, issuer=issuer)])
    assert "not signed by exactly one CA" in check(site).errors[0]


def test_a_crl_without_a_crl_number_is_an_error(site):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, number=None)])
    assert only_error(check(site)).endswith("the CRL has no CRL Number")


@pytest.mark.parametrize(("ahead", "error"), [(timedelta(minutes=5), False), (timedelta(minutes=5, seconds=1), True)])
def test_a_last_update_more_than_5_minutes_ahead_is_an_error(site, ahead, error):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    result = check(site, NOW - DAY - ahead)
    assert (len(result.errors), "ahead of this host's clock" in str(result.errors)) == (
        (1, True) if error else (0, False)
    )


def test_the_ca_is_expired_at_exactly_its_not_after(site):
    ca = make_ca(days=10)
    place(site.public, [ca], [make_crl(ca, days_left=20)])
    assert only_error(check(site, ca.cert.not_valid_after_utc)).endswith("the CA has expired")


def resigned(tbs: bytes, key: ec.EllipticCurvePrivateKey) -> bytes:
    def tlv(tag: int, body: bytes) -> bytes:
        size = len(body)
        long_form = size.to_bytes((size.bit_length() + 7) // 8, "big")
        return bytes([tag]) + (bytes([size]) if size < 128 else bytes([0x80 | len(long_form)]) + long_form) + body

    alg = tlv(0x30, tlv(0x06, bytes.fromhex("2a8648ce3d040302")))  # ecdsa-with-SHA256
    der = tlv(0x30, tbs + alg + tlv(0x03, b"\0" + key.sign(tbs, ec.ECDSA(hashes.SHA256()))))
    return b"-----BEGIN X509 CRL-----\n" + base64.encodebytes(der) + b"-----END X509 CRL-----\n"


def test_a_signed_crl_whose_fields_fail_to_parse_after_loading_is_a_typed_error(site):
    ca = make_ca()
    alt_name = x509.IssuerAlternativeName([x509.DNSName("crl.example.com")])
    tbs = crl_builder(ca).add_extension(alt_name, critical=False).sign(ca.key, hashes.SHA256()).tbs_certlist_bytes
    issuer_alt_oid, crl_number_oid = bytes.fromhex("0603551d12"), bytes.fromhex("0603551d14")
    place(site.public, [ca], [resigned(tbs.replace(issuer_alt_oid, crl_number_oid), ca.key)])  # CRL Number twice
    assert only_error(check(site)).endswith("the CRL is not a valid CRL")
    assert not site.record.exists()


def test_only_the_ca_whose_crl_has_no_error_is_recorded(site):
    good, bad = make_ca("good CA"), make_ca("bad CA")
    place(site.public, [good, bad], [make_crl(good, number=4), make_crl(bad, number=9, days_left=-1)])
    result = check(site)
    assert len(result.errors) == 1 and "the CRL has expired" in result.errors[0]
    assert json.loads(site.record.read_text())["crls"] == {crl_check.ca_id(good.cert): 4}


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b'{"version": 2, "crls": {}}',
        b'{"version": 1, "crls": {"ab": 1}}',
        b'{"version": true, "crls": {}}',
        b'{"version": 1, "crls": {}, "x": 1}',
        ('{"version": 1, "crls": {"%s": -1}}' % ("a" * 64)).encode(),
        json.dumps({"version": 1, "crls": {"a" * 16: 2**159}}).encode(),
    ],
)
def test_a_malformed_record_is_an_error_naming_it_and_is_never_rewritten(site, raw):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    site.state.mkdir(mode=0o700)
    site.record.write_bytes(raw)
    os.chmod(site.record, 0o600)
    assert only_error(check(site)).startswith("crl-seen.json: malformed")
    assert site.record.read_bytes() == raw


def test_a_symlinked_or_group_readable_record_is_an_error(site, tmp_path: Path):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    assert check(site).errors == ()
    os.chmod(site.record, 0o640)
    assert "no group/other access" in only_error(check(site))
    real = tmp_path / "elsewhere.json"
    real.write_bytes(site.record.read_bytes())
    os.chmod(real, 0o600)
    site.record.unlink()
    site.record.symlink_to(real)
    assert only_error(check(site)).startswith("crl-seen.json: cannot open safely")


def test_a_shared_state_dir_is_refused_before_the_record_is_written(site):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    site.state.mkdir(mode=0o750)
    os.chmod(site.state, 0o750)  # noqa: S103 - a group-readable state dir is the case under test
    assert "state dir: must be owned by the plugin user" in only_error(check(site))
    assert not site.record.exists()


def test_the_bundle_ca_order_does_not_matter_and_the_pem_helpers_round_trip(site):
    new, previous = make_ca("new CA"), make_ca("previous CA")
    place(site.public, [previous, new], [make_crl(new), make_crl(previous)])
    assert check(site) == crl_check.CrlCheck((), ())
    loaded = crl_check.load_cas(pem(new.cert) + pem(previous.cert), "x")
    assert [c.public_bytes(serialization.Encoding.DER) for c in loaded] == [
        c.cert.public_bytes(serialization.Encoding.DER) for c in (new, previous)
    ]
