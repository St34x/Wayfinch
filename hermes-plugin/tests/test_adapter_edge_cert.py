# SPDX-License-Identifier: Apache-2.0
"""The forwarded certificate as ``adapter.connect()`` wires it (T1.184c): the checker reads the signer's
``device-ca.pem`` next to ``devices.json``. Through the real unix listener: the enrolled device's certificate
authenticates and the verified fingerprint wins over a forged ``X-Client-Cert-Fp``; the fingerprint header alone, or a
certificate of a CA outside the bundle, is ``401``; a bundle that is missing, a symlink or group-writable refuses every
certificate; a CA removed from the bundle stops vouching after the re-read and vouches again once added back."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from cert_helpers import device_cert, make_ca, pem_escaped
from edge_helpers import raw_unix, request_bytes, short_dir
from server_auth_helpers import CERT_VALUES, FP, OTHER_FP, TEST_CA, upgrade_headers
from signer_registry_helpers import publish

import test_adapter
from test_adapter import new_adapter
from test_adapter_edge_registry import _extra, _state, as_signer  # noqa: F401 - the fixture is used by name

adapter_module = test_adapter.adapter_module  # the fixture of test_adapter, used by name as a test argument


def _request(token: str, headers: dict[str, str]) -> bytes:
    return request_bytes("GET", "/ws", {**upgrade_headers(), "Authorization": f"Bearer {token}", **headers})


def _statuses(tmp_path: Path, adapter_module, token: str, header_sets, between=None) -> list[bytes]:
    with short_dir() as d:
        adapter = new_adapter(adapter_module, _extra(d / "edge.sock", tmp_path))

        async def run() -> list[bytes]:
            assert await adapter.connect() is True
            answers = []
            for number, headers in enumerate(header_sets):
                if between is not None and number:
                    between(number)
                answers.append((await raw_unix(d / "edge.sock", _request(token, headers))).split(b"\r\n", 1)[0])
            await adapter.disconnect()
            return answers

        return asyncio.run(run())


def test_the_enrolled_certificate_authenticates_and_the_fingerprint_header_counts_for_nothing(
    adapter_module,
    tmp_path,
    as_signer,  # noqa: F811 - the imported fixture, used by name
):
    _target, _device, token = publish(tmp_path)
    _state(tmp_path)
    foreign = pem_escaped(device_cert(make_ca("glasses device CA other")))
    sets = [
        {"X-Client-Cert": CERT_VALUES[FP], "X-Client-Cert-Fp": OTHER_FP},  # the verified fingerprint wins
        {"X-Client-Cert-Fp": FP},  # the enrolled fingerprint, but no certificate
        {"X-Client-Cert": foreign},  # a certificate of a CA outside the bundle
        {"X-Client-Cert": CERT_VALUES[OTHER_FP]},  # a good certificate of a device that is not enrolled
    ]
    answers = _statuses(tmp_path, adapter_module, token, sets)
    assert answers[0].startswith(b"HTTP/1.1 101")
    assert all(a.startswith(b"HTTP/1.1 401") for a in answers[1:]), answers


@pytest.mark.parametrize("damage", ["missing", "symlink", "group_writable", "empty"])
def test_an_unusable_ca_bundle_refuses_every_certificate(adapter_module, tmp_path, as_signer, damage):  # noqa: F811 - the imported fixture, used by name
    target, _device, token = publish(tmp_path)
    bundle = target.with_name("device-ca.pem")
    if damage == "missing":
        bundle.unlink()
    elif damage == "symlink":
        bundle.rename(target.with_name("elsewhere.pem"))
        bundle.symlink_to("elsewhere.pem")
    elif damage == "group_writable":
        bundle.chmod(0o660)
    else:
        bundle.write_bytes(b"")
    _state(tmp_path)
    assert _statuses(tmp_path, adapter_module, token, [{"X-Client-Cert": CERT_VALUES[FP]}])[0].startswith(
        b"HTTP/1.1 401"
    )


def test_a_ca_removed_from_the_bundle_stops_vouching_and_vouches_again_once_added_back(
    adapter_module,
    tmp_path,
    as_signer,  # noqa: F811 - the imported fixture, used by name
):
    target, _device, token = publish(tmp_path)
    bundle = target.with_name("device-ca.pem")
    other = make_ca("glasses device CA next")
    bundle.write_bytes(TEST_CA.pem + other.pem)  # a rotation overlap: two CAs
    _state(tmp_path)

    def rotate(number: int) -> None:
        bundle.write_bytes(other.pem if number == 1 else other.pem + TEST_CA.pem)
        time.sleep(1.1)  # the bundle is looked at again at most once a second

    sets = [{"X-Client-Cert": CERT_VALUES[FP]}] * 3
    answers = _statuses(tmp_path, adapter_module, token, sets, between=rotate)
    assert [a.split(b" ")[1] for a in answers] == [b"101", b"401", b"101"]
