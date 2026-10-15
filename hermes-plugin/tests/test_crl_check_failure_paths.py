# SPDX-License-Identifier: Apache-2.0
"""Failure paths of ``glasses-admin crl --check`` on the signer's published bundles: empty, binary, truncated and
unreadable files, a directory in place of a file, CRL expiry and rollback boundaries, a tampered signature, the
same-subject rotation overlap, and ``crl-seen.json`` that is foreign, oversized, read-only or raised concurrently."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from signer_bundles import DAY, NOW, crl_builder, make_ca, make_crl, make_site, pem, place, run_check

from glasses import crl_check
from glasses import signer_protocol as proto


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def recorded(site) -> dict[str, int]:
    return json.loads(site.record.read_text())["crls"]


def leftovers(site) -> list[str]:
    return sorted(p.name for p in site.state.iterdir() if p.name != crl_check.RECORD_NAME)


def write_record(site, numbers: dict[str, int]) -> None:
    site.state.mkdir(mode=0o700, exist_ok=True)
    site.record.write_text(json.dumps({"version": 1, "crls": numbers}))
    os.chmod(site.record, 0o600)


def _truncated(data: bytes) -> bytes:
    return data[: len(data) // 2]


@pytest.mark.parametrize("file", [proto.PUBLIC_CA_BUNDLE, proto.PUBLIC_CRL])
@pytest.mark.parametrize(
    "damage",
    [lambda d: b"", lambda d: b"\n \n", lambda d: os.urandom(512), _truncated, lambda d: d.replace(b"-----END", b"")],
    ids=["empty", "blank", "binary", "truncated", "no-end-line"],
)
def test_an_empty_binary_or_truncated_published_file_is_an_error_naming_it(site, monkeypatch, file, damage):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    path = site.public / file
    path.write_bytes(damage(path.read_bytes()))
    code, out = run_check(monkeypatch, site)
    assert code == 1 and f"error: crl --check: {file}: not a valid PEM" in out
    assert str(site.public) not in out and not site.record.exists()


def test_a_der_crl_instead_of_pem_is_an_error(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    der = crl_builder(ca).sign(ca.key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)
    (site.public / proto.PUBLIC_CRL).write_bytes(der)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "devices-crl.pem: not a valid PEM CRL bundle" in out


@pytest.mark.parametrize("extra", [b"junk text\n", "half"], ids=["text", "half-cert"])
def test_a_ca_bundle_with_junk_or_half_a_certificate_after_the_ca_is_an_error(site, monkeypatch, extra):
    ca, other = make_ca("A"), make_ca("B")
    place(site.public, [ca], [make_crl(ca)])
    tail = _truncated(pem(other.cert)) if extra == "half" else extra
    (site.public / proto.PUBLIC_CA_BUNDLE).write_bytes(pem(ca.cert) + tail)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "device-ca.pem: not a valid PEM" in out


@pytest.mark.parametrize("file", [proto.PUBLIC_CA_BUNDLE, proto.PUBLIC_CRL])
def test_a_directory_in_place_of_a_published_file_is_an_error(site, monkeypatch, file):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    (site.public / file).unlink()
    (site.public / file).mkdir()
    code, out = run_check(monkeypatch, site)
    assert code == 1 and f"error: crl --check: {file}: not a regular file" in out


def test_a_directory_in_place_of_a_published_file_leaks_no_fd(tmp_path):
    (tmp_path / "dir").mkdir()
    fds = len(os.listdir("/proc/self/fd"))
    for _ in range(50):
        with pytest.raises(crl_check.CheckError, match="not a regular file"):
            crl_check.read_published(tmp_path / "dir")
    assert len(os.listdir("/proc/self/fd")) == fds


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-0 file")
def test_an_unreadable_published_file_is_an_error(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    os.chmod(site.public / proto.PUBLIC_CRL, 0)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "devices-crl.pem: cannot open safely (PermissionError)" in out


def test_a_crl_with_a_flipped_signature_byte_is_an_error_also_in_a_world_writable_file(site, monkeypatch):
    ca = make_ca()
    crl = crl_builder(ca).sign(ca.key, hashes.SHA256())
    der = bytearray(crl.public_bytes(serialization.Encoding.DER))
    der[-1] ^= 0x01  # last byte of the signature value
    tampered = x509.load_der_x509_crl(bytes(der)).public_bytes(serialization.Encoding.PEM)
    place(site.public, [ca], [tampered])
    os.chmod(site.public / proto.PUBLIC_CRL, 0o666)  # noqa: S103 - the mode is not what protects the file
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "not signed by exactly one CA" in out and "0 CRLs in devices-crl.pem" in out


def test_a_crl_is_expired_at_exactly_its_next_update_and_warns_one_second_before(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, days_left=10)])
    code, out = run_check(monkeypatch, site, NOW + 10 * DAY - timedelta(seconds=1))
    assert code == 3 and "the CRL expires in 0 days" in out
    code, out = run_check(monkeypatch, site, NOW + 10 * DAY)
    assert code == 1 and "the CRL has expired" in out and "WARN" not in out


def test_an_error_keeps_the_warnings_of_its_ca_and_exits_1(site, monkeypatch):
    ca = make_ca(days=100)  # would warn: within 180 days
    place(site.public, [ca], [make_crl(ca, days_left=-1)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl check: FAILED (1 error(s), 1 warning(s))" in out and "plan a CA rotation" in out


def test_both_warnings_together_exit_3(site, monkeypatch):
    ca = make_ca(days=30)
    place(site.public, [ca], [make_crl(ca, days_left=5)])
    code, out = run_check(monkeypatch, site)
    assert code == 3 and "crl check: WARN (0 error(s), 2 warning(s))" in out


def test_the_record_is_keyed_by_the_ca_id_of_the_ca_spki(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, number=12)])
    assert run_check(monkeypatch, site)[0] == 0
    spki = ca.cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key = hashlib.sha256(spki).hexdigest()[:16]
    assert recorded(site) == {key: 12}
    assert os.stat(site.record).st_mode & 0o777 == 0o600


def test_an_equal_crl_number_is_no_rollback_and_a_lower_one_in_one_ca_leaves_the_other_raised(site, monkeypatch):
    a, b = make_ca("A"), make_ca("B")
    ka, kb = crl_check.ca_id(a.cert), crl_check.ca_id(b.cert)
    write_record(site, {ka: 5, kb: 5})
    place(site.public, [a, b], [make_crl(a, number=5), make_crl(b, number=5)])
    assert run_check(monkeypatch, site)[0] == 0 and recorded(site) == {ka: 5, kb: 5}
    place(site.public, [a, b], [make_crl(a, number=4), make_crl(b, number=9)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "the CRL number 4 is lower than the recorded 5" in out
    assert recorded(site) == {ka: 5, kb: 9}


def test_a_record_of_a_retired_ca_is_kept(site, monkeypatch):
    old, new = make_ca("old"), make_ca("new")
    write_record(site, {crl_check.ca_id(old.cert): 40})
    place(site.public, [new], [make_crl(new, number=1)])
    assert run_check(monkeypatch, site)[0] == 0
    assert recorded(site) == {crl_check.ca_id(old.cert): 40, crl_check.ca_id(new.cert): 1}


def test_a_rotation_overlap_with_two_cas_of_the_same_subject_is_clean(site, monkeypatch):
    previous, current = make_ca("device CA"), make_ca("device CA")
    place(site.public, [previous, current], [make_crl(current, number=2), make_crl(previous, number=8)])
    assert run_check(monkeypatch, site)[0] == 0
    assert recorded(site) == {crl_check.ca_id(previous.cert): 8, crl_check.ca_id(current.cert): 2}


def test_a_foreign_owned_record_is_an_error_and_is_not_rewritten(site, monkeypatch):
    ca = make_ca()
    write_record(site, {crl_check.ca_id(ca.cert): 1})
    before = site.record.read_bytes()
    place(site.public, [ca], [make_crl(ca, number=9)])
    real_uid = os.geteuid()
    monkeypatch.setattr(crl_check.os, "geteuid", lambda: real_uid + 1)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl-seen.json: must be owned by the plugin user" in out
    assert site.record.read_bytes() == before


def test_an_oversized_record_is_an_error(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    site.state.mkdir(mode=0o700)
    site.record.write_bytes(b" " * (crl_check.PUBLISHED_CAP + 1))
    os.chmod(site.record, 0o600)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl-seen.json: larger than" in out


def test_a_directory_in_place_of_the_record_is_an_error(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    site.state.mkdir(mode=0o700)
    site.record.mkdir(mode=0o700)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl-seen.json: not a regular file" in out


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only dir")
def test_a_read_only_state_dir_fails_the_raise_without_leftovers_but_not_an_unchanged_record(site, monkeypatch):
    ca = make_ca()
    key = crl_check.ca_id(ca.cert)
    write_record(site, {key: 7})
    place(site.public, [ca], [make_crl(ca, number=7)])
    os.chmod(site.state, 0o500)
    try:
        assert run_check(monkeypatch, site)[0] == 0  # nothing to raise, nothing written
        place(site.public, [ca], [make_crl(ca, number=8)])
        code, out = run_check(monkeypatch, site)
        assert code == 1 and "error: crl --check: crl-seen.json: cannot write it (PermissionError)" in out
        assert recorded(site) == {key: 7} and leftovers(site) == []
    finally:
        os.chmod(site.state, 0o700)


def test_a_failed_rename_leaves_the_old_record_and_no_temp_file(site, monkeypatch):
    ca = make_ca()
    key = crl_check.ca_id(ca.cert)
    write_record(site, {key: 2})
    place(site.public, [ca], [make_crl(ca, number=3)])

    def refuse(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(crl_check.os, "replace", refuse)
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "crl-seen.json: cannot write it (OSError)" in out
    assert recorded(site) == {key: 2} and leftovers(site) == []


def test_concurrent_raises_keep_the_highest_number_and_a_valid_record(site):
    keys = [hashlib.sha256(bytes([i])).hexdigest()[:16] for i in range(3)]
    record = crl_check.CrlRecord(site.state)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            crl_check.CrlRecord(site.state).raise_to({key: n for key in keys})
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in (*range(1, 41), *range(40, 0, -1))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and record.read() == {key: 40 for key in keys} and leftovers(site) == []


def test_concurrent_checks_of_two_numbers_end_on_the_higher_one(site, monkeypatch):
    ca = make_ca()
    monkeypatch.setattr(crl_check, "_utcnow", lambda: NOW)
    dirs = []
    for number in (10, 11):
        public = site.public.parent / f"public-{number}"
        place(public, [ca], [make_crl(ca, number=number)])
        dirs.append(public)
    results: list[crl_check.CrlCheck] = []

    def run(public) -> None:
        for _ in range(10):
            ca_path, crl_path = public / proto.PUBLIC_CA_BUNDLE, public / proto.PUBLIC_CRL
            results.append(crl_check.check_published(ca_path, crl_path, site.state))

    threads = [threading.Thread(target=run, args=(d,)) for d in dirs * 3]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert recorded(site) == {crl_check.ca_id(ca.cert): 11} and leftovers(site) == []
    rolled = [r for r in results if r.errors]
    assert all("the CRL number 10 is lower than the recorded 11" in r.errors[0] for r in rolled)
