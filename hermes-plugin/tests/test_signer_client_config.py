# SPDX-License-Identifier: Apache-2.0
"""The signer client's config keys (``signer.socket`` counted in UTF-8 bytes), the signer public files, the conftest
signer guard (SAT-SC-10: no test reaches a real signer socket or public dir) and the fake signer's refusal of ops
outside the seven."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import fake_signer
import pytest

from glasses import config, signer_client
from glasses import signer_protocol as proto
from glasses.signer_client import SignerClient

from conftest import GUARDED_STORES, StateDirOutsideTmpError


@pytest.fixture
def fake():
    with fake_signer.running() as running:
        yield running


# Config keys and public files


def cfg(**signer: str):
    return config.load({"signer": signer})


@pytest.mark.parametrize("missing", ["socket", "user"])
def test_a_client_needs_both_signer_keys(missing):
    values = {"socket": "/run/signer/s.sock", "user": "signer"}
    del values[missing]
    with pytest.raises(signer_client.NotConfigured, match=f"signer.{missing} is not set"):
        signer_client.client_from_config(cfg(**values))


@pytest.mark.parametrize("uid", [0, os.geteuid()], ids=["root", "own-uid"])
def test_a_signer_user_that_is_root_or_the_plugin_itself_is_refused(monkeypatch, uid):
    monkeypatch.setattr(signer_client.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=uid))
    with pytest.raises(signer_client.SignerUserRefused):
        signer_client.client_from_config(cfg(socket="/run/signer/s.sock", user="signer"))


def test_an_unknown_signer_user_is_refused(monkeypatch):
    def unknown(_name: str):
        raise KeyError(_name)

    monkeypatch.setattr(signer_client.pwd, "getpwnam", unknown)
    with pytest.raises(signer_client.SignerUserRefused):
        signer_client.client_from_config(cfg(socket="/run/signer/s.sock", user="signer"))


def test_a_configured_client_uses_the_socket_and_the_users_uid(monkeypatch, fake):
    monkeypatch.setattr(signer_client.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=os.geteuid() + 7))
    made = signer_client.client_from_config(cfg(socket=str(fake.socket_path), user="signer"))
    assert (made.socket_path, made.signer_uid) == (fake.socket_path, os.geteuid() + 7)


def test_public_file_names_only_the_contract_files(tmp_path):
    settings = cfg(public_dir=str(tmp_path))
    for name in (proto.PUBLIC_CA_BUNDLE, proto.PUBLIC_CRL, proto.PUBLIC_REGISTRY):
        assert signer_client.public_file(settings, name) == tmp_path / name
    with pytest.raises(signer_client.UnknownPublicFile):
        signer_client.public_file(settings, "../ca.key")
    with pytest.raises(signer_client.NotConfigured, match="signer.public_dir is not set"):
        signer_client.public_file(cfg(), proto.PUBLIC_CRL)


# Test guard


def test_the_guard_fails_a_client_for_a_socket_outside_the_allowed_temp_dirs():
    with pytest.raises(StateDirOutsideTmpError, match="signer path"):
        SignerClient(Path("/run/device-signer/signer.sock"), 1)


def test_the_guard_fails_a_public_file_outside_the_allowed_temp_dirs():
    with pytest.raises(StateDirOutsideTmpError, match="signer path"):
        signer_client.public_file(cfg(public_dir="/var/lib/device-signer/public"), proto.PUBLIC_CRL)


def test_the_guard_allows_the_fake_signers_directory_only_while_it_runs():
    with fake_signer.running() as fake:
        SignerClient(fake.socket_path, os.geteuid())
    with pytest.raises(StateDirOutsideTmpError):
        SignerClient(fake.socket_path, os.geteuid())


def test_the_guard_covers_the_registry_store():
    assert ("glasses.registry", "Registry") in GUARDED_STORES


def test_the_guard_resolves_a_symlinked_parent_directory_of_the_socket(tmp_path):
    (tmp_path / "d").symlink_to("/run/device-signer")
    with pytest.raises(StateDirOutsideTmpError, match="signer path"):
        SignerClient(tmp_path / "d" / "signer.sock", 1)


def test_the_guard_resolves_a_symlinked_public_dir(tmp_path):
    (tmp_path / "pub").symlink_to("/var/lib/device-signer/public")
    with pytest.raises(StateDirOutsideTmpError, match="signer path"):
        signer_client.public_file(cfg(public_dir=str(tmp_path / "pub")), proto.PUBLIC_REGISTRY)


def test_the_guard_checks_the_public_dir_at_its_source():
    with pytest.raises(StateDirOutsideTmpError, match="signer path"):
        signer_client._public_dir(cfg(public_dir="/var/lib/device-signer/public"))


# signer.socket fits an AF_UNIX address: 107 bytes in UTF-8, not 107 characters


def socket_errors(path: str) -> list[str]:
    return config.check({"signer": {"socket": path}}).errors


def test_a_socket_path_of_exactly_107_bytes_is_accepted():
    for path in ("/" + "s" * 106, "/" + "\u00e9" * 53):
        assert len(path.encode()) == 107 and socket_errors(path) == []


@pytest.mark.parametrize(
    "path",
    ["/" + "s" * 107, "/s" + "\u00e9" * 53, "/" + "\u00e9" * 106],
    ids=["108-ascii", "108-bytes-55-chars", "107-chars-213-bytes"],
)
def test_a_socket_path_over_107_utf8_bytes_is_refused(path):
    (error,) = socket_errors(path)
    assert "107 bytes" in error


# The fake signer answers like the real one to an op outside the seven


def test_the_fake_signer_answers_forbidden_to_an_op_outside_the_seven(fake):
    line = json.dumps({"v": 1, "op": "window_open", "rid": "a" * 32}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(5.0)
        sock.connect(str(fake.socket_path))
        sock.sendall(line)
        answer = sock.makefile("rb").readline()
    assert json.loads(answer) == {"v": 1, "rid": "a" * 32, "ok": False, "error": "forbidden"}
    assert fake.ops == ["window_open"]
