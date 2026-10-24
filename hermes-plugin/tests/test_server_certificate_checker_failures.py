# SPDX-License-Identifier: Apache-2.0
"""``/ws`` when the certificate checker misbehaves or the gate is closed (T1.184c): a checker that raises, or answers
something that is not a fingerprint string, is the fixed ``401`` with no gate call and no registry read, and the
failure is logged once per exception type with the type name only (a message with certificate-looking text stays
out). A refused certificate is judged before a closed gate (``401``, the gate is not asked); a good certificate with
a closed gate is ``503``. A revoked device's own certificate is ``403`` whatever fingerprint a forged header names.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from server_auth_helpers import FP, OTHER_FP, creds
from server_helpers import Wiring, wiring

from glasses.server import build_app

from test_server_peer_and_log import one_get, recording

SECRET_TEXT = "-----BEGIN CERTIFICATE-----MIIBsecretsecret"


class _Str(str):
    """A str subclass: not an exact str, so never a fingerprint."""


def _app(w: Wiring, *, fingerprint_of: Any, gate_open: bool = True) -> tuple[web.Application, list[str]]:
    events, spy = recording(w)

    async def gate() -> bool:
        events.append("gate")
        return gate_open

    def certificate(request: web.BaseRequest) -> Any:
        events.append("cert")
        return fingerprint_of(request)

    deps = w.deps(registry=spy, gate_ok_async=gate, client_fingerprint=certificate)
    return build_app(deps), events


def _get(app: web.Application, token: str) -> tuple[int, bytes]:
    return asyncio.run(one_get(app, "/ws", {"Authorization": f"Bearer {token}"}))


def _raises(_request: web.BaseRequest) -> str:
    raise RuntimeError(SECRET_TEXT)


@pytest.mark.parametrize("bad", [None, 7, b"ab" * 32, ["ab" * 32], _Str("ab" * 32)], ids=lambda v: type(v).__name__)
def test_an_answer_that_is_not_a_fingerprint_string_is_401_before_the_gate(tmp_path, bad):
    w = wiring(tmp_path)
    app, events = _app(w, fingerprint_of=lambda _r: bad)
    status, body = _get(app, w.token)
    assert (status, body) == (401, b'{"error":"unauthorized"}')
    assert events == ["cert"]  # no gate call, no registry call


@pytest.mark.parametrize("answer", ["", "not-a-fingerprint", "AB" * 31], ids=["empty", "not-hex", "too-short"])
def test_a_string_that_is_not_a_fingerprint_costs_no_registry_read(tmp_path, answer):
    w = wiring(tmp_path)
    app, events = _app(w, fingerprint_of=lambda _r: answer)
    assert _get(app, w.token)[0] == 401
    assert "lookup" not in events


def test_a_raising_checker_is_401_before_the_gate_and_logged_by_type_only(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    w = wiring(tmp_path)
    app, events = _app(w, fingerprint_of=_raises)
    assert _get(app, w.token) == (401, b'{"error":"unauthorized"}')
    assert events == ["cert"]
    assert "RuntimeError" in caplog.text and SECRET_TEXT not in caplog.text
    assert SECRET_TEXT not in "".join(str(r.args) + str(r.exc_info) for r in caplog.records)


def test_a_repeating_checker_failure_warns_once_and_warns_again_after_a_success(tmp_path, caplog):
    w = wiring(tmp_path)
    answers: list[Any] = ["raise", "raise", FP, "raise"]

    def checker(_request: web.BaseRequest) -> str:
        answer = answers.pop(0)
        if answer == "raise":
            raise RuntimeError(SECRET_TEXT)
        return answer

    app, _events = _app(w, fingerprint_of=checker)
    caplog.set_level(logging.DEBUG)

    async def four_requests() -> None:
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            for _ in range(4):
                async with client.get("/ws", headers={"Authorization": f"Bearer {w.token}"}) as response:
                    assert response.status == 401

    asyncio.run(four_requests())
    levels = [r.levelno for r in caplog.records if "certificate check failed" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG, logging.WARNING]


def test_a_refused_certificate_is_judged_before_a_closed_gate(tmp_path):
    w = wiring(tmp_path)
    app, events = _app(w, fingerprint_of=lambda _r: None, gate_open=False)
    assert _get(app, w.token)[0] == 401
    assert events == ["cert"]  # the gate was not asked


def test_a_verified_certificate_with_a_closed_gate_is_503(tmp_path):
    w = wiring(tmp_path)
    app, events = _app(w, fingerprint_of=lambda _r: FP, gate_open=False)
    status, _body = _get(app, w.token)
    assert status == 503
    assert events == ["cert", "gate"]  # the registry is not read while the gate is closed


def test_a_revoked_devices_own_certificate_is_403_whatever_fingerprint_a_header_names(tmp_path):
    w = wiring(tmp_path)
    other, _token = w.registry.add("second", OTHER_FP, ["main"])
    w.registry.revoke(w.device.device_id)
    app = build_app(w.deps())
    forged = {**creds(w.token, FP), "X-Client-Cert-Fp": other.cert_fingerprint}
    status, _body = asyncio.run(one_get(app, "/ws", forged))
    assert status == 403  # the verified fingerprint is the revoked one; the header naming the active device is ignored
