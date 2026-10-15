# SPDX-License-Identifier: Apache-2.0
"""``crl --check`` bundle and record edges: the CA bundle accepts CRLF line ends and surrounding whitespace but no other
PEM block kind, the CRL bundle refuses a private key block without echoing it, a record keyed by anything but a
16-hex ``ca_id`` is malformed, and a CA whose public key algorithm the library cannot load is an error line, not a
traceback."""

from __future__ import annotations

import json
import os

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from signer_bundles import make_ca, make_crl, make_site, pem, place, run_check

from glasses import signer_protocol as proto


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def test_a_ca_bundle_with_crlf_line_ends_and_surrounding_whitespace_is_clean(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    (site.public / proto.PUBLIC_CA_BUNDLE).write_bytes(b"\n  \n" + pem(ca.cert).replace(b"\n", b"\r\n") + b"\n\t\n")
    code, out = run_check(monkeypatch, site)
    assert code == 0 and "crl check: OK" in out


@pytest.mark.parametrize("kind", ["TRUSTED CERTIFICATE", "X509 CRL", "PUBLIC KEY"])
def test_a_ca_bundle_with_another_pem_block_kind_is_an_error(site, monkeypatch, kind):
    ca = make_ca()
    crl = make_crl(ca)
    place(site.public, [ca], [crl])
    body = pem(ca.cert).split(b"\n", 1)[1].rsplit(b"-----END", 1)[0]
    other = f"-----BEGIN {kind}-----\n".encode() + body + f"-----END {kind}-----\n".encode()
    (site.public / proto.PUBLIC_CA_BUNDLE).write_bytes(pem(ca.cert) + other)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "device-ca.pem: not a valid PEM certificate bundle" in out
    assert not site.record.exists()


def test_a_private_key_block_in_the_crl_bundle_is_an_error_that_never_echoes_it(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    key_pem = ca.key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    (site.public / proto.PUBLIC_CRL).write_bytes(make_crl(ca) + key_pem)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "devices-crl.pem: not a valid PEM CRL bundle" in out
    assert "PRIVATE KEY" not in out and key_pem.decode().splitlines()[1] not in out


@pytest.mark.parametrize("key", ["a" * 64, "0123456789ABCDEF", "0123456789abcde", "0123456789abcdef0"])
def test_a_record_keyed_by_anything_but_a_16_hex_ca_id_is_malformed_and_kept(site, monkeypatch, key):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    site.state.mkdir(mode=0o700)
    text = json.dumps({"version": 1, "crls": {key: 3}})
    site.record.write_text(text)
    os.chmod(site.record, 0o600)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl-seen.json: malformed" in out
    assert site.record.read_text() == text


def _ca_with_patched_spki(old: bytes, new: bytes) -> tuple[bytes, bytes]:
    """A CA cert whose SPKI names an algorithm or curve the library cannot load, and a CRL of the original key."""
    ca = make_ca("unloadable key CA", key=ec.generate_private_key(ec.SECP256R1()))
    der = ca.cert.public_bytes(serialization.Encoding.DER)
    assert der.count(old) == 1
    patched = x509.load_der_x509_certificate(der.replace(old, new))
    return patched.public_bytes(serialization.Encoding.PEM), make_crl(ca)


UNLOADABLE = {  # DER of the OID: id-ecPublicKey 1.2.840.10045.2.1, prime256v1 1.2.840.10045.3.1.7
    "unknown-key-algorithm": (bytes.fromhex("06072a8648ce3d0201"), bytes.fromhex("06072a8648ce3d0209")),
    "unknown-curve": (bytes.fromhex("06082a8648ce3d030107"), bytes.fromhex("06082a8648ce3d030106")),
}


@pytest.mark.parametrize("case", sorted(UNLOADABLE))
def test_a_ca_with_an_unloadable_public_key_is_an_error_line_not_a_traceback(site, monkeypatch, case):
    ca_pem, crl = _ca_with_patched_spki(*UNLOADABLE[case])
    site.public.mkdir()
    (site.public / proto.PUBLIC_CA_BUNDLE).write_bytes(ca_pem)
    (site.public / proto.PUBLIC_CRL).write_bytes(crl)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl check: FAILED" in out
    assert "device-ca.pem: a certificate in it has a public key the check cannot read" in out
    assert not site.record.exists()
