# SPDX-License-Identifier: Apache-2.0
"""``glasses-admin crl --check`` on the signer's published CA and CRL bundles: the expiry warnings and errors, missing
and corrupt files, CRL rollback against the plugin's ``crl-seen.json``, foreign signatures, the two-CA rotation
overlap, unsafe file types, and the test guard over the record and the published paths."""

from __future__ import annotations

import json
import os
import stat

import pytest
from signer_bundles import DAY, NOW, make_ca, make_crl, make_site, place, run_check

from glasses import crl_check
from glasses import signer_protocol as proto

from conftest import StateDirOutsideTmpError


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def recorded(site) -> dict[str, int]:
    return json.loads(site.record.read_text())["crls"]


@pytest.mark.parametrize(("days_left", "code", "word"), [(15, 0, "OK"), (14, 3, "WARN")])
def test_a_crl_warns_at_14_days_left_and_not_at_15(site, monkeypatch, days_left, code, word):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, days_left=days_left)])
    got, out = run_check(monkeypatch, site)
    assert got == code and f"crl check: {word} (0 error(s), {code // 3} warning(s))" in out
    assert ("warning: crl --check:" in out) == (code == 3)


def test_an_expired_crl_is_an_error_and_exits_1(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, days_left=1)])
    code, out = run_check(monkeypatch, site, NOW + 2 * DAY)
    assert code == 1 and "the CRL has expired" in out and "crl check: FAILED" in out


@pytest.mark.parametrize(("days_left", "warns"), [(180, True), (181, False)])
def test_the_ca_warns_at_180_days_left_and_not_at_181(site, monkeypatch, days_left, warns):
    ca = make_ca(days=days_left)
    place(site.public, [ca], [make_crl(ca)])
    code, out = run_check(monkeypatch, site)
    assert (code, "the CA expires in 180 days" in out) == ((3, True) if warns else (0, False))


@pytest.mark.parametrize("missing", [proto.PUBLIC_CA_BUNDLE, proto.PUBLIC_CRL])
def test_a_missing_published_file_is_an_error(site, monkeypatch, missing):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    (site.public / missing).unlink()
    code, out = run_check(monkeypatch, site)
    assert code == 1 and f"error: crl --check: {missing}: missing" in out and str(site.public) not in out


@pytest.mark.parametrize("corrupt", [proto.PUBLIC_CA_BUNDLE, proto.PUBLIC_CRL])
def test_a_corrupt_pem_is_an_error(site, monkeypatch, corrupt):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    (site.public / corrupt).write_bytes(b"-----BEGIN X509 CRL-----\nnot base64 at all\n-----END X509 CRL-----\n")
    code, out = run_check(monkeypatch, site)
    assert code == 1 and f"error: crl --check: {corrupt}: not a valid PEM" in out


def test_a_lower_crl_number_than_recorded_is_an_error_and_the_record_stays(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca, number=9)])
    assert run_check(monkeypatch, site)[0] == 0
    before = site.record.read_bytes()
    place(site.public, [ca], [make_crl(ca, number=8)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "the CRL number 8 is lower than the recorded 9" in out
    assert site.record.read_bytes() == before


def test_a_crl_signed_by_another_key_is_an_error(site, monkeypatch):
    ca, other = make_ca(), make_ca()
    place(site.public, [ca], [make_crl(ca, signer=other)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "not signed by exactly one CA" in out and "1 CRLs" not in out
    assert not site.record.exists()


def test_a_two_ca_bundle_with_one_crl_each_is_clean_and_both_are_recorded(site, monkeypatch):
    new, previous = make_ca("new CA"), make_ca("previous CA")
    place(site.public, [new, previous], [make_crl(previous, number=3), make_crl(new, number=5)])
    code, out = run_check(monkeypatch, site)
    assert code == 0 and "crl check: OK (0 error(s), 0 warning(s))" in out
    assert sorted(recorded(site).values()) == [3, 5]


def test_a_ca_without_its_crl_is_an_error(site, monkeypatch):
    new, previous = make_ca("new CA"), make_ca("previous CA")
    place(site.public, [new, previous], [make_crl(new)])
    code, out = run_check(monkeypatch, site)
    assert code == 1 and "0 CRLs in devices-crl.pem, exactly 1 expected" in out


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
@pytest.mark.parametrize("file", [proto.PUBLIC_CA_BUNDLE, proto.PUBLIC_CRL])
def test_a_symlink_or_fifo_in_place_of_a_published_file_is_an_error(site, monkeypatch, tmp_path, kind, file):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    target = site.public / file
    real = tmp_path / "real.pem"
    real.write_bytes(target.read_bytes())
    target.unlink()
    if kind == "symlink":
        target.symlink_to(real)
    else:
        os.mkfifo(target)
    code, out = run_check(monkeypatch, site)
    reason = "cannot open safely (OSError)" if kind == "symlink" else "not a regular file"
    assert code == 1 and f"error: crl --check: {file}: {reason}" in out


def test_published_files_of_mode_0640_are_no_error_and_the_record_is_0600(site, monkeypatch):
    ca = make_ca()
    place(site.public, [ca], [make_crl(ca)])
    assert stat.S_IMODE(os.stat(site.public / proto.PUBLIC_CRL).st_mode) == 0o640
    assert run_check(monkeypatch, site)[0] == 0
    assert stat.S_IMODE(os.stat(site.record).st_mode) == 0o600


def test_the_record_is_only_ever_raised(site, monkeypatch):
    ca = make_ca()
    for number, code in ((4, 0), (6, 0), (5, 1), (6, 0)):
        place(site.public, [ca], [make_crl(ca, number=number)])
        assert run_check(monkeypatch, site)[0] == code
        assert list(recorded(site).values()) == [6 if number != 4 else 4]
    assert list(recorded(site)) == [crl_check.ca_id(ca.cert)]


def test_an_unset_public_dir_exits_2_naming_the_key(tmp_path, monkeypatch):
    site = make_site(tmp_path, public_dir="")
    code, out = run_check(monkeypatch, site)
    assert code == 2 and "signer.public_dir is not set" in out


def test_the_note_follows_the_summary_on_every_run(site, monkeypatch):
    for days_left in (29, 3, -1):
        ca = make_ca()
        place(site.public, [ca], [make_crl(ca, days_left=days_left)])
        out = run_check(monkeypatch, site)[1].splitlines()
        assert out[-2].startswith("crl check: ")
        assert out[-1] == "note: the signer's published CRL was checked; the edge copy is not read here"


def test_the_guard_covers_the_crl_record():
    with pytest.raises(StateDirOutsideTmpError):
        crl_check.CrlRecord("/var/lib/glasses-state")


def test_the_published_files_resolve_through_the_guarded_public_file(tmp_path, monkeypatch):
    site = make_site(tmp_path, public_dir="/var/lib/device-signer/public")
    with pytest.raises(StateDirOutsideTmpError):
        run_check(monkeypatch, site)
