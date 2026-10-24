# SPDX-License-Identifier: Apache-2.0
"""Decoding of the forwarded certificate header and the CA bundle reader of ``edge_cert`` (T1.184c).

Header: ``pem-urlescaped`` (nginx ``$ssl_client_escaped_cert``) and ``der-base64``, one certificate exactly; garbage,
an empty value, a value over the 8 KiB cap (checked before decoding), a PEM with trailing data, a chain of two certs,
non-canonical base64 are refused. Bundle: ``device-ca.pem`` read through the signer-file reader (owner, mode 0640 or
stricter, regular file, no symlink); a symlink, a group-writable or foreign-owned file, a missing or empty bundle
refuse every cert; a CA removed from the bundle stops vouching after the re-read and vouches again once added back;
nothing is cached past a failed read; at most one read per second.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import pytest
from cert_helpers import NOW, der, der_base64, device_cert, fingerprint, key_usage, make_ca, pem_escaped
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from glasses import edge_cert, registry_signer_file
from glasses.edge_cert import MAX_HEADER_BYTES, CaBundle, CertRefused

CA_A = make_ca("glasses device CA aaaa")
CA_B = make_ca("glasses device CA bbbb")
CERT = device_cert(CA_A)
PEM = CERT.public_bytes(serialization.Encoding.PEM).decode("ascii")
_UNKNOWN = x509.UnrecognizedExtension(x509.ObjectIdentifier("1.3.6.1.4.1.55555.1"), b"\x05\x00")


@pytest.mark.parametrize(
    ("value", "encoding"),
    [(pem_escaped(CERT), "pem-urlescaped"), (der_base64(CERT), "der-base64")],
    ids=["pem-urlescaped", "der-base64"],
)
def test_both_encodings_decode_to_the_der(value: str, encoding: str):
    assert edge_cert.decode(value, encoding) == der(CERT)


BAD_PEM = {
    "empty": "",
    "garbage": quote("-----BEGIN CERTIFICATE-----\nnot base64 at all\n-----END CERTIFICATE-----\n", safe=""),
    "trailing-data": quote(PEM + "junk", safe=""),
    "leading-data": quote("junk\n" + PEM, safe=""),
    "two-certs": quote(PEM + device_cert(CA_B).public_bytes(serialization.Encoding.PEM).decode(), safe=""),
    "private-key-block": quote(PEM.replace("CERTIFICATE", "PRIVATE KEY"), safe=""),
    "not-escaped-invalid-percent": "%ZZ" + pem_escaped(CERT),
    "non-ascii-escape": "%FF" + pem_escaped(CERT),
    "padding-inside": quote(
        "\n".join(line + "=" if i == 1 else line for i, line in enumerate(PEM.split("\n"))), safe=""
    ),
}


@pytest.mark.parametrize("value", BAD_PEM.values(), ids=BAD_PEM.keys())
def test_bad_pem_values_are_refused(value: str):
    with pytest.raises(CertRefused):
        edge_cert.decode(value, "pem-urlescaped")


BAD_DER = {
    "empty": "",
    "whitespace": der_base64(CERT)[:10] + " " + der_base64(CERT)[10:],
    "urlsafe-alphabet": der_base64(CERT).replace("+", "-").replace("/", "_") + "-_",
    "truncated": der_base64(CERT)[:-1],
    "non-canonical-pad-bits": "QR==",
    "pem-instead": pem_escaped(CERT),
}


@pytest.mark.parametrize("value", BAD_DER.values(), ids=BAD_DER.keys())
def test_bad_base64_values_are_refused(value: str):
    with pytest.raises(CertRefused):
        edge_cert.decode(value, "der-base64")


def test_the_length_cap_is_checked_before_decoding(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(edge_cert, "unquote", lambda *a, **k: calls.append("unquote") or "")
    monkeypatch.setattr(edge_cert.base64, "b64decode", lambda *a, **k: calls.append("b64decode") or b"")
    for encoding in ("pem-urlescaped", "der-base64"):
        with pytest.raises(CertRefused):
            edge_cert.decode("A" * (MAX_HEADER_BYTES + 1), encoding)
    assert calls == [] and MAX_HEADER_BYTES == 8192


def test_a_value_of_exactly_the_cap_is_decoded_not_refused_for_length(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(edge_cert.base64, "b64decode", lambda value, **k: calls.append(len(value)) or b"")
    with pytest.raises(CertRefused):  # refused later (not a certificate), but it was decoded
        edge_cert.decode("A" * MAX_HEADER_BYTES, "der-base64")
    assert calls == [MAX_HEADER_BYTES]


# --- the CA bundle -----------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _public(tmp_path: Path, *cas, mode: int = 0o640) -> Path:
    public = tmp_path / "public"
    public.mkdir(mode=0o750, exist_ok=True)
    bundle = public / "device-ca.pem"
    bundle.write_bytes(b"".join(ca.pem for ca in cas))
    bundle.chmod(mode)
    return public


def _bundle(public: Path, clock: Clock | None = None) -> CaBundle:
    return CaBundle(public, os.geteuid(), monotonic=clock or Clock())


def test_the_bundle_is_read_with_both_cas(tmp_path):
    cas = _bundle(_public(tmp_path, CA_A, CA_B)).cas()
    assert [ca.subject for ca in cas] == [CA_A.cert.subject, CA_B.cert.subject]


def _symlinked(tmp_path: Path) -> Path:
    public = _public(tmp_path, CA_A)
    (public / "device-ca.pem").rename(public / "real.pem")
    (public / "device-ca.pem").symlink_to("real.pem")
    return public


def _missing(tmp_path: Path) -> Path:
    public = _public(tmp_path, CA_A)
    (public / "device-ca.pem").unlink()
    return public


UNUSABLE = {
    "symlink": _symlinked,
    "group-writable": lambda p: _public(p, CA_A, mode=0o660),
    "world-readable": lambda p: _public(p, CA_A, mode=0o644),
    "missing": _missing,
    "empty": lambda p: _public(p),
    "no-directory": lambda p: p / "absent",
}


@pytest.mark.parametrize("make", UNUSABLE.values(), ids=UNUSABLE.keys())
def test_an_unusable_bundle_refuses_every_cert(tmp_path, make):
    with pytest.raises(CertRefused):
        _bundle(make(tmp_path)).cas()


def test_a_bundle_owned_by_another_uid_refuses_every_cert(tmp_path):
    public = _public(tmp_path, CA_A)
    with pytest.raises(CertRefused):
        CaBundle(public, os.geteuid() + 1, monotonic=Clock()).cas()


def test_the_bundle_is_read_through_the_signer_file_reader(tmp_path, monkeypatch):
    seen: list[tuple[Path, int, str]] = []
    real = registry_signer_file.read_signer_file

    def spy(directory: Path, uid: int, name: str) -> bytes:
        seen.append((directory, uid, name))
        return real(directory, uid, name)

    monkeypatch.setattr(edge_cert, "read_signer_file", spy)
    public = _public(tmp_path, CA_A)
    _bundle(public).cas()
    assert seen == [(public, os.geteuid(), "device-ca.pem")]


BAD_BUNDLES = {
    "text-outside-blocks": lambda: b"hello\n" + CA_A.pem,
    "private-key-block": lambda: CA_A.pem.replace(b"CERTIFICATE", b"PRIVATE KEY"),
    "a-device-cert": lambda: device_cert(CA_A).public_bytes(serialization.Encoding.PEM),
    "five-cas": lambda: b"".join(make_ca(f"ca {i}").pem for i in range(5)),
    "broken-base64": lambda: CA_A.pem.replace(b"M", b"!", 1),
    "ca-without-key-usage": lambda: make_ca("ca x", usage=None).pem,
    "ca-without-key-cert-sign": lambda: make_ca("ca x", usage=key_usage("crl_sign")).pem,
    "ca-unknown-critical-extension": lambda: make_ca("ca x", extra=((_UNKNOWN, True),)).pem,
    "ca-key-on-secp256k1": lambda: make_ca("ca x", key=ec.generate_private_key(ec.SECP256K1())).pem,
    "ca-sha224-self-signature": lambda: make_ca("ca x", digest=hashes.SHA224()).pem,
}


def test_a_ca_with_a_weak_self_signature_hash_is_refused_for_that_hash_alone():
    key = ec.generate_private_key(ec.SECP256R1())
    weak, strong = make_ca("ca x", key=key, digest=hashes.SHA224()), make_ca("ca x", key=key)
    assert isinstance(weak.cert.signature_hash_algorithm, hashes.SHA224)  # the vector is what it claims to be
    assert len(edge_cert.parse_bundle(strong.pem)) == 1  # same key, name, profile and curve: only the hash differs
    with pytest.raises(CertRefused):
        edge_cert.parse_bundle(weak.pem)


@pytest.mark.parametrize("content", BAD_BUNDLES.values(), ids=BAD_BUNDLES.keys())
def test_an_invalid_bundle_refuses_every_cert(tmp_path, content):
    public = _public(tmp_path, CA_A)
    (public / "device-ca.pem").write_bytes(content())
    with pytest.raises(CertRefused):
        _bundle(public).cas()


class _NoPattern:
    def findall(self, raw: bytes) -> list[bytes]:
        raise AssertionError("a pattern ran over a bundle over the size cap")

    sub = findall


def test_the_bundle_size_cap_is_checked_before_any_pattern_runs(monkeypatch):
    padded = CA_A.pem + b"\n" * (edge_cert.MAX_BUNDLE_BYTES - len(CA_A.pem))
    assert edge_cert.parse_bundle(padded)  # exactly the cap: parsed
    monkeypatch.setattr(edge_cert, "_BUNDLE_BLOCK", _NoPattern())
    with pytest.raises(CertRefused):
        edge_cert.parse_bundle(padded + b"\n")


def test_a_removed_ca_stops_vouching_after_the_re_read_and_vouches_again_once_added_back(tmp_path):
    clock = Clock()
    public = _public(tmp_path, CA_A, CA_B)
    bundle = _bundle(public, clock)
    cert_b = device_cert(CA_B)
    cert = der(cert_b)
    assert edge_cert.verify(cert, bundle.cas(), NOW) == fingerprint(cert_b)
    (public / "device-ca.pem").write_bytes(CA_A.pem)
    clock.now += 0.5
    assert edge_cert.verify(cert, bundle.cas(), NOW)  # within a second: no re-read yet
    clock.now += 0.6
    with pytest.raises(CertRefused):
        edge_cert.verify(cert, bundle.cas(), NOW)
    (public / "device-ca.pem").write_bytes(CA_A.pem + CA_B.pem)
    clock.now += 1.1
    assert edge_cert.verify(cert, bundle.cas(), NOW)


def test_nothing_is_cached_past_a_failed_read(tmp_path):
    clock = Clock()
    public = _public(tmp_path, CA_A)
    bundle = _bundle(public, clock)
    assert bundle.cas()
    (public / "device-ca.pem").chmod(0o660)  # loose mode: same mtime and size, only the mode changed
    clock.now += 1.1
    with pytest.raises(CertRefused):
        bundle.cas()
    (public / "device-ca.pem").chmod(0o640)
    clock.now += 0.5
    with pytest.raises(CertRefused):  # within the second after a failure: still refused, the old CAs are gone
        bundle.cas()
    clock.now += 0.6
    assert bundle.cas()


def test_a_bundle_removed_after_a_good_read_refuses_every_cert(tmp_path):
    clock = Clock()
    public = _public(tmp_path, CA_A)
    bundle = _bundle(public, clock)
    assert bundle.cas()
    (public / "device-ca.pem").unlink()
    clock.now += 1.1
    with pytest.raises(CertRefused):
        bundle.cas()


def test_an_unchanged_bundle_is_not_re_read_and_reads_happen_at_most_once_a_second(tmp_path, monkeypatch):
    reads: list[str] = []
    real = registry_signer_file.read_signer_file
    monkeypatch.setattr(edge_cert, "read_signer_file", lambda *a: reads.append("read") or real(*a))
    clock = Clock()
    bundle = _bundle(_public(tmp_path, CA_A), clock)
    for _ in range(50):
        bundle.cas()
        clock.now += 0.1
    assert reads == ["read"]  # 5 s of calls, the file never changed


def test_a_failed_read_is_retried_at_most_once_a_second(tmp_path, monkeypatch):
    reads: list[str] = []
    real = registry_signer_file.read_signer_file
    monkeypatch.setattr(edge_cert, "read_signer_file", lambda *a: reads.append("read") or real(*a))
    clock = Clock()
    bundle = _bundle(_public(tmp_path, CA_A, mode=0o666), clock)
    for _ in range(25):
        with pytest.raises(CertRefused):
            bundle.cas()
        clock.now += 0.1
    assert 2 <= len(reads) <= 3
