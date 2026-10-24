# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a): one read of the registry file per request (it can be 4 MiB), whether the
request is admitted, has a wrong token or a wrong fingerprint, or names a tombstoned fingerprint."""

from __future__ import annotations

import pytest
from server_auth_helpers import FP, OTHER_FP, always, creds, enrolled, get, make_app, refusal

from glasses.registry import Registry
from glasses.server_auth import AuthDeps

WRONG_TOKEN = "w" * 43


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Names of the file reads (``Registry._open_data`` opens the file once per read)."""
    opened: list[str] = []
    real = Registry._open_data

    def counting(self: Registry) -> int | None:
        opened.append("open")
        return real(self)

    monkeypatch.setattr(Registry, "_open_data", counting)
    return opened


def deps_of(registry: Registry) -> AuthDeps:
    return AuthDeps(gate_ok_async=always(True), registry=registry, is_revoked=lambda _id: False)


def test_an_admitted_request_reads_the_registry_once(tmp_path, reads):
    registry, device, token = enrolled(tmp_path)
    handled: list[str] = []
    reads.clear()  # enrolling read the file too
    reply = get(make_app(deps_of(registry), handled), creds(token))
    assert reply.status == 101 and handled == [device.device_id], reply
    assert len(reads) == 1


@pytest.mark.parametrize("case", ["wrong token", "other fingerprint", "tombstoned fingerprint"])
def test_a_failing_request_reads_the_registry_once(tmp_path, reads, case):
    registry, device, token = enrolled(tmp_path)
    if case == "tombstoned fingerprint":
        registry.revoke(device.device_id)
    reads.clear()
    headers = {
        "wrong token": creds(WRONG_TOKEN),
        "other fingerprint": creds(token, OTHER_FP),
        "tombstoned fingerprint": creds(token),
    }[case]
    reply = get(make_app(deps_of(registry), []), headers)
    expected = (403, "device_revoked") if case == "tombstoned fingerprint" else (401, "unauthorized")
    assert refusal(reply, *expected), reply
    assert len(reads) == 1


def test_lookup_answers_the_device_and_the_tombstone(tmp_path):
    registry, device, token = enrolled(tmp_path)
    assert registry.lookup(token, FP) == (device, False)
    assert registry.lookup(WRONG_TOKEN, FP) == (None, False)
    assert registry.lookup(token, OTHER_FP) == (None, False)
    assert registry.lookup(None, FP) == (None, False) and registry.lookup(token, None) == (None, False)
    registry.revoke(device.device_id)
    assert registry.lookup(token, FP)[1] is True and registry.lookup(token, FP)[0] is None
    assert registry.lookup(WRONG_TOKEN, FP) == (None, True)  # the tombstone answers whatever the token
