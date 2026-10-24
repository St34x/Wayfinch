# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a): every refusal is an HTTP answer before any WebSocket exists."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from server_auth_helpers import (
    FP,
    OTHER_FP,
    SpyRegistry,
    cert_header,
    creds,
    deps_for,
    enrolled,
    get,
    make_app,
    refusal,
)

from glasses.registry import Device
from glasses.registry_parse import RegistryCorruptError
from glasses.server_auth import AuthDeps, _header

UNAUTHORIZED = (401, "unauthorized")
UNAVAILABLE = (503, "unavailable")
REVOKED = (403, "device_revoked")


def tamper(tmp_path: Path, change) -> None:
    path = tmp_path / "state" / "devices.json"
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def test_valid_credentials_complete_the_upgrade(tmp_path):
    registry, device, token = enrolled(tmp_path)
    handled: list[str] = []

    async def main() -> int:
        app = make_app(deps_for(registry), handled)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            ws = await client.ws_connect("/ws", headers=creds(token))
            await ws.close()
            return ws.close_code or 0

    assert asyncio.run(main()) == 1000
    assert handled == [device.device_id]


@pytest.mark.parametrize("revoked", [False, True])
def test_a_refusal_reaches_the_client_as_a_failed_handshake_not_a_websocket(tmp_path, revoked):
    registry, device, token = enrolled(tmp_path)
    handled: list[str] = []
    ids = frozenset({device.device_id}) if revoked else frozenset()
    sent = creds(token) if revoked else {"X-Client-Cert-Fp": FP}  # the old fingerprint header: no longer read

    async def main() -> int:
        app = make_app(deps_for(registry, revoked=ids), handled)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            with pytest.raises(WSServerHandshakeError) as refused:
                await client.ws_connect("/ws", headers=sent)
            return refused.value.status

    assert asyncio.run(main()) == (403 if revoked else 401)
    assert handled == []


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda t, fp: cert_header(fp), id="no-authorization"),
        pytest.param(lambda t, fp: {"Authorization": f"Bearer {t}"}, id="no-certificate"),
        pytest.param(lambda t, fp: creds("x" * 43, fp), id="wrong-token"),
        pytest.param(lambda t, fp: creds(t, OTHER_FP), id="fingerprint-mismatch"),
        pytest.param(lambda t, fp: {"Authorization": f"Bearer {t}", "X-Client-Cert-Fp": fp}, id="fingerprint-header"),
        pytest.param(lambda t, fp: {"Authorization": f"Basic {t}", **cert_header(fp)}, id="not-bearer"),
        pytest.param(lambda t, fp: {"Authorization": f"Bearer  {t}", **cert_header(fp)}, id="two-spaces"),
        pytest.param(lambda t, fp: {"Authorization": "Bearer ", **cert_header(fp)}, id="empty-token"),
        pytest.param(lambda t, fp: creds("a" * 600, fp), id="token-over-cap"),
        pytest.param(
            lambda t, fp: {"Authorization": f"Bearer {t}", "X-Client-Cert": "a" * 600}, id="not-a-certificate"
        ),
    ],
)
def test_missing_or_invalid_credentials_give_401_with_a_fixed_body(tmp_path, mutate):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []
    reply = get(make_app(deps_for(registry), handled), mutate(token, FP))
    assert refusal(reply, *UNAUTHORIZED), reply
    assert handled == []
    assert b"device_revoked" not in reply.body


def test_the_header_cap_is_512_bytes_and_counts_bytes_not_characters():
    exact = "Bearer " + "a" * 505  # 512 bytes: passes the cap (the token shape refuses it later, no registry read)
    over = "Bearer " + "a" * 506
    wide = "Bearer " + "é" * 253  # 253 characters, 513 bytes
    read = [
        _header(make_mocked_request("GET", "/ws", headers={"Authorization": v}), "Authorization")
        for v in (exact, over, wide)
    ]
    assert read == [exact, None, None]
    spy = SpyRegistry()
    for value in (exact, over, wide):
        assert refusal(get(make_app(deps_for(spy), []), {"Authorization": value, **cert_header()}), *UNAUTHORIZED)
    assert spy.calls == []  # no value of this size is a token: none reaches the registry


def test_a_header_sent_twice_is_refused_not_picked(tmp_path):
    registry, _device, token = enrolled(tmp_path)

    async def main():
        async with TestClient(TestServer(make_app(deps_for(registry), []), host="127.0.0.1")) as client:
            headers = [
                ("Authorization", f"Bearer {token}"),
                ("Authorization", "Bearer other"),
                *cert_header().items(),
            ]
            async with client.get("/ws", headers=headers) as response:
                return response.status

    assert asyncio.run(main()) == 401


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda d: d["devices"][0].__setitem__("certFingerprint", None), id="null-fingerprint"),
        pytest.param(lambda d: d["devices"][0].__setitem__("certFingerprint", 7), id="number-fingerprint"),
        pytest.param(lambda d: d.__setitem__("version", 99), id="unknown-version"),
        pytest.param(lambda d: d.__setitem__("devices", "x"), id="devices-not-a-list"),
    ],
)
def test_a_corrupt_registry_file_gives_401_and_only_the_error_type_is_logged(tmp_path, caplog, change):
    registry, _device, token = enrolled(tmp_path)
    tamper(tmp_path, change)
    reply = get(make_app(deps_for(registry), []), creds(token))
    assert refusal(reply, *UNAUTHORIZED)
    logged = [r for r in caplog.records if "registry error" in r.getMessage()]
    assert [r.getMessage() for r in logged] == ["glasses ws: registry error (RegistryCorruptError); refusing"]


def test_a_garbage_or_unsafe_registry_file_gives_401(tmp_path):
    registry, _device, token = enrolled(tmp_path)
    path = tmp_path / "state" / "devices.json"
    path.write_bytes(b"{not json")
    assert refusal(get(make_app(deps_for(registry), []), creds(token)), *UNAUTHORIZED)
    os.chmod(path, 0o644)
    assert refusal(get(make_app(deps_for(registry), []), creds(token)), *UNAUTHORIZED)


@pytest.mark.parametrize("error", [RegistryCorruptError("x"), OSError("disk"), PermissionError("no")])
def test_any_registry_error_or_os_error_fails_closed(tmp_path, error):
    app = make_app(deps_for(SpyRegistry(error=error)), [])
    assert refusal(get(app, creds("t" * 43)), *UNAUTHORIZED)


def test_a_revoked_device_gets_403_with_the_marker_on_every_revoked_path(tmp_path):
    registry, device, token = enrolled(tmp_path)
    handled: list[str] = []
    # 1. the injected is_revoked (valid credentials, the device is revoked in memory)
    injected = make_app(deps_for(registry, revoked=frozenset({device.device_id})), handled)
    assert refusal(get(injected, creds(token)), *REVOKED)
    # 2. a registry revokedAt entry that still holds its token hash
    kept = Device(device.device_id, "n", FP, (), (), "t", None, "2026-10-17T00:00:00Z")
    assert refusal(get(make_app(deps_for(SpyRegistry(device=kept)), handled), creds(token)), *REVOKED)
    # 3. the fingerprint tombstone left by Registry.revoke
    registry.revoke(device.device_id)
    assert refusal(get(make_app(deps_for(registry), handled), creds(token)), *REVOKED)
    assert handled == []


def test_a_tombstone_fingerprint_is_403_with_a_wrong_token_too(tmp_path):
    registry, device, _token = enrolled(tmp_path)
    registry.revoke(device.device_id)
    reply = get(make_app(deps_for(registry), []), creds("x" * 43))
    assert refusal(reply, *REVOKED)
    other = get(make_app(deps_for(registry), []), creds("x" * 43, OTHER_FP))
    assert refusal(other, *UNAUTHORIZED)


def test_the_marker_is_only_on_the_revoked_answer(tmp_path):
    registry, device, token = enrolled(tmp_path)
    marked = []
    for deps, headers in [
        (deps_for(registry), {}),
        (deps_for(registry), creds("x" * 43)),
        (deps_for(registry, gate=False), creds(token)),
        (deps_for(SpyRegistry(error=OSError())), creds(token)),
        (deps_for(registry, revoked=frozenset({device.device_id})), creds(token)),
    ]:
        reply = get(make_app(deps, []), headers)
        if b"device_revoked" in reply.body or b"device_revoked" in str(reply.headers).encode():
            marked.append(reply.status)
    assert marked == [403]


def test_gate_false_gives_503_before_the_registry_is_read(tmp_path):
    spy = SpyRegistry()
    reply = get(make_app(deps_for(spy, gate=False), []), creds("t" * 43))
    assert refusal(reply, *UNAVAILABLE)
    assert spy.calls == []
    # even with no token: the gate is the first step of authenticate (the certificate precedes it in the server)
    assert refusal(get(make_app(deps_for(spy, gate=False), []), cert_header()), *UNAVAILABLE)


def test_a_gate_check_that_raises_gives_503(tmp_path, caplog):
    async def broken() -> bool:
        raise RuntimeError("secret-detail")

    registry, _device, token = enrolled(tmp_path)
    deps = deps_for(registry)
    deps = type(deps)(gate_ok_async=broken, registry=registry, is_revoked=deps.is_revoked)
    assert refusal(get(make_app(deps, []), creds(token)), *UNAVAILABLE)
    assert "secret-detail" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.parametrize("answer", [None, 1, "yes", [True]])
def test_only_a_true_gate_answer_opens_the_upgrade(tmp_path, answer):
    async def odd() -> object:
        return answer

    registry, _device, token = enrolled(tmp_path)
    deps = AuthDeps(gate_ok_async=odd, registry=registry, is_revoked=lambda device_id: False)  # type: ignore[arg-type]
    assert refusal(get(make_app(deps, []), creds(token)), *UNAVAILABLE)


def test_gate_refusal_is_one_debug_line_per_request_with_no_value(tmp_path, caplog):
    """Not a WARNING per request (the adapter logs the gate on change); never a token or a header."""
    registry, _device, token = enrolled(tmp_path)
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        get(make_app(deps_for(registry, gate=False), []), creds(token))
    ours = [r for r in caplog.records if r.name == "glasses.server_auth"]
    line = "glasses ws: upgrade refused: allow-all gate"
    assert [(r.levelno, r.getMessage()) for r in ours] == [(logging.DEBUG, line)] * 3
    assert token not in caplog.text and FP not in caplog.text


def test_a_token_issued_for_one_certificate_is_401_with_another(tmp_path):
    """SAT-CA-55: the issued pair opens the upgrade; the same token with another certificate does not."""
    registry, device, token = enrolled(tmp_path)
    handled: list[str] = []
    assert refusal(get(make_app(deps_for(registry), handled), creds(token, OTHER_FP)), *UNAUTHORIZED)
    assert handled == []
    registry.add("second", OTHER_FP, ["main"])  # the other device's cert is no key to this token
    assert refusal(get(make_app(deps_for(registry), handled), creds(token, OTHER_FP)), *UNAUTHORIZED)
    assert get(make_app(deps_for(registry), handled), creds(token, FP)).status == 101
    assert handled == [device.device_id]
