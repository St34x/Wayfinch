# SPDX-License-Identifier: Apache-2.0
"""Test helper: CA and CRL bundles in the signer's published format (``device-ca.pem``, ``devices-crl.pem``), built
with test-only keys made per test, plus a config naming the public dir and a ``crl --check`` runner with a set clock."""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from glasses import admin, crl_check
from glasses import signer_protocol as proto

NOW = datetime(2026, 10, 11, 1, 2, 3, tzinfo=UTC)
DAY = timedelta(days=1)


@dataclass(frozen=True)
class Ca:
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate


def name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def make_ca(cn: str = "test device CA", days: float = 3650, is_ca: bool = True, key=None) -> Ca:
    key = key or ec.generate_private_key(ec.SECP256R1())
    builder = x509.CertificateBuilder().subject_name(name(cn)).issuer_name(name(cn)).public_key(key.public_key())
    builder = builder.serial_number(x509.random_serial_number()).not_valid_before(NOW - 400 * DAY)
    builder = builder.not_valid_after(NOW + days * DAY)
    builder = builder.add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
    return Ca(key, builder.sign(key, hashes.SHA256()))


def crl_builder(ca: Ca, number: int | None = 7, days_left: float = 29, issuer: x509.Name | None = None):
    builder = x509.CertificateRevocationListBuilder().issuer_name(ca.cert.subject if issuer is None else issuer)
    builder = builder.last_update(NOW - DAY).next_update(NOW + days_left * DAY)
    if number is not None:
        builder = builder.add_extension(x509.CRLNumber(number), critical=False)
    return builder


def make_crl(ca: Ca, number: int | None = 7, days_left: float = 29, **kwargs) -> bytes:
    signer = kwargs.pop("signer", ca)
    builder = kwargs.pop("builder", None) or crl_builder(ca, number, days_left, **kwargs)
    return builder.sign(signer.key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def place(public_dir: Path, cas: list[Ca], crls: list[bytes]) -> None:
    public_dir.mkdir(exist_ok=True)
    for file, data in (
        (proto.PUBLIC_CA_BUNDLE, b"".join(pem(c.cert) for c in cas)),
        (proto.PUBLIC_CRL, b"".join(crls)),
    ):
        path = public_dir / file
        path.write_bytes(data)
        os.chmod(path, 0o640)  # the published mode (brief section 12)


@dataclass(frozen=True)
class Site:
    """One test's signer public dir, plugin state dir and config file."""

    public: Path
    state: Path
    config: Path

    @property
    def record(self) -> Path:
        return self.state / crl_check.RECORD_NAME


def make_site(tmp_path: Path, public_dir: str | None = None) -> Site:
    public, state = tmp_path / "public", tmp_path / "state"
    shown = str(public) if public_dir is None else public_dir
    config = tmp_path / "config.yaml"
    lines = ["gateway:", "  platforms:", "    glasses:", "      extra:", f"        state_dir: {state}"]
    lines += ["        signer:", f"          public_dir: '{shown}'"]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Site(public, state, config)


def run_check(monkeypatch: pytest.MonkeyPatch, site: Site, when: datetime = NOW) -> tuple[int, str]:
    monkeypatch.setattr(crl_check, "_utcnow", lambda: when)
    out = io.StringIO()
    return admin.main(["crl", "--check", "--config", str(site.config)], env={}, out=out), out.getvalue()
