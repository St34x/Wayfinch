# SPDX-License-Identifier: Apache-2.0
"""``crl --check``: the CA bundle holds only certificates, a CA is named and recorded by the signer's ``ca_id`` (SPKI
hash, so a re-issued cert keeps its rollback record), a damaged record never hides the CA and CRL checks, warnings
show next to errors, the recorded number never goes down, and bad CAs or out-of-range CRL Numbers are errors."""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from signer_bundles import DAY, NOW, Ca, make_ca, make_crl, make_site, pem, place, run_check

from glasses import crl_check
from glasses import signer_protocol as proto


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def write_record(site, text: str, mode: int = 0o600) -> None:
    site.state.mkdir(mode=0o700, exist_ok=True)
    site.record.write_text(text)
    os.chmod(site.record, mode)


def reissued(ca: Ca, days: float) -> Ca:
    """A new CA cert over the same key and subject, with another validity (as a re-issue would make)."""
    return Ca(ca.key, make_ca(ca.cert.subject.rfc4514_string()[3:], days=days, key=ca.key).cert)


def test_a_private_key_block_in_the_ca_bundle_is_an_error_that_never_echoes_it(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    key_pem = ca.key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    (site.public / proto.PUBLIC_CA_BUNDLE).write_bytes(pem(ca.cert) + key_pem)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "device-ca.pem: not a valid PEM certificate bundle" in out
    assert "PRIVATE KEY" not in out and not site.record.exists()


def test_the_ca_label_is_the_signer_ca_id_of_the_spki(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, days_left=-1)])
    spki = ca.cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    code, out = run_check(monkeypatch, site)
    assert code == 1 and f"CA {hashlib.sha256(spki).hexdigest()[:16]}: the CRL has expired" in out


def test_a_reissued_ca_cert_over_the_same_key_keeps_its_recorded_number(site, monkeypatch):
    ca = make_ca("device CA", days=3650)
    place(site.public, [ca], [make_crl(ca, number=10)])
    assert run_check(monkeypatch, site)[0] == 0
    again = reissued(ca, days=3000)
    assert again.cert.fingerprint(hashes.SHA256()) != ca.cert.fingerprint(hashes.SHA256())
    place(site.public, [again], [make_crl(again, number=3)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "the CRL number 3 is lower than the recorded 10" in out
    assert json.loads(site.record.read_text())["crls"] == {crl_check.ca_id(ca.cert): 10}


@pytest.mark.parametrize(
    ("text", "mode", "line"),
    [
        ("{not json", 0o600, "crl-seen.json: malformed"),
        ('{"version": 1, "crls": {}}', 0o640, "crl-seen.json: must be owned by the plugin user"),
        ("[" * 200_000, 0o600, "crl-seen.json: malformed"),
    ],
    ids=["malformed", "group-readable", "deeply-nested"],
)
def test_a_damaged_record_still_reports_an_expired_crl_and_is_not_rewritten(site, monkeypatch, text, mode, line):
    ca = make_ca(days=100)
    place(site.public, [ca], [make_crl(ca, days_left=-1)])
    write_record(site, text, mode)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and line in out and "the CRL has expired" in out and "plan a CA rotation" in out
    assert "crl check: FAILED (2 error(s), 1 warning(s))" in out and site.record.read_text() == text


def test_a_damaged_record_with_a_clean_crl_is_one_error_and_no_raise(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, number=4)])
    write_record(site, "{}")
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl check: FAILED (1 error(s), 0 warning(s))" in out and site.record.read_text() == "{}"


def test_a_directory_in_place_of_the_record_with_a_bad_ca_bundle_reports_both(site, monkeypatch):
    place(site.public, [make_ca()], [])
    (site.public / proto.PUBLIC_CA_BUNDLE).write_bytes(b"junk\n")
    site.state.mkdir(mode=0o700)
    site.record.mkdir()
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "device-ca.pem: not a valid PEM" in out and "crl-seen.json: not a regular file" in out


def test_a_rollback_error_keeps_the_crl_expiry_warning(site, monkeypatch):
    ca = make_ca()
    write_record(site, json.dumps({"version": 1, "crls": {crl_check.ca_id(ca.cert): 10}}))
    place(site.public, [ca], [make_crl(ca, number=5, days_left=3)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "lower than the recorded 10" in out and "the CRL expires in 3 days" in out


def test_a_lower_raise_never_lowers_the_recorded_number(site):
    record = crl_check.CrlRecord(site.state)
    record.raise_to({"0123456789abcdef": 10})
    record.raise_to({"0123456789abcdef": 3, "fedcba9876543210": 1})
    assert record.read() == {"0123456789abcdef": 10, "fedcba9876543210": 1}


@pytest.mark.parametrize("number", [1 << 159, (1 << 159) + 12345])
def test_a_crl_number_beyond_20_octets_is_an_error_and_not_recorded(site, monkeypatch, number):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, number=number)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "the CRL Number is outside 0..2^159" in out and not site.record.exists()


def test_the_largest_20_octet_crl_number_is_recorded(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, number=(1 << 159) - 1)])
    assert run_check(monkeypatch, site)[0] == 0
    assert crl_check.CrlRecord(site.state).read() == {crl_check.ca_id(ca.cert): (1 << 159) - 1}


def test_a_ca_not_yet_valid_is_an_error(site, monkeypatch):
    ca = make_ca()  # valid from NOW - 400 days
    place(site.public, [ca], [make_crl(ca)])
    code, out = run_check(monkeypatch, site, NOW - 401 * DAY)
    assert code == 1 and "the CA is not yet valid: check the clock" in out


def test_a_ca_with_a_broken_self_signature_is_an_error(site, monkeypatch):
    ca = make_ca()
    der = bytearray(ca.cert.public_bytes(serialization.Encoding.DER))
    der[-1] ^= 0x01  # last byte of the signature value
    broken = Ca(ca.key, x509.load_der_x509_certificate(bytes(der)))
    place(site.public, [broken], [make_crl(ca)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "the CA's self-signature does not verify" in out and not site.record.exists()
