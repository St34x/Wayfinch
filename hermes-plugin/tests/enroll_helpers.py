# SPDX-License-Identifier: Apache-2.0
"""Shared helpers of the ``/enroll`` relay tests: the submitted fields, a relay against the fake signer, an app that
mounts it for every method, and loopback HTTP exchanges. A test module uses the ``fake`` fixture by binding it at
module level (``fake = enroll_helpers.fake``)."""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from unittest import mock

import fake_signer
import pytest
from aiohttp import streams, web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from glasses.enroll import EnrollRelay, RateLimiter
from glasses.signer_client import SignerClient, Timeouts

SPKI = base64.b64encode(b"spki-sentinel-key-bytes!").decode()
CODE = base64.urlsafe_b64encode(b"code-sentinel-16").decode().rstrip("=")
SIG = base64.b64encode(b"sig-sentinel-bytes").decode()
BODY = {"spki": SPKI, "code": CODE, "signature": SIG}
SUBMITTED = (SPKI, CODE, SIG)
JSON = {"Content-Type": "application/json"}


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Mapping[str, str]
    body: bytes


@pytest.fixture
def fake(tmp_path: object) -> Iterator[fake_signer.FakeSigner]:
    # tmp_path first: the conftest signer guard reads it from client threads (KB thread race)
    with fake_signer.running() as running:
        yield running


def factory(fake: fake_signer.FakeSigner, enrol_s: float = 2.0) -> Callable[[], SignerClient]:
    timeouts = Timeouts(connect_s=1.0, op_s=1.0, enrol_s=enrol_s)
    return lambda: SignerClient(fake.socket_path, os.geteuid(), timeouts=timeouts)


def relay_for(fake: fake_signer.FakeSigner, **kwargs: Any) -> EnrollRelay:
    kwargs.setdefault("limiter", RateLimiter(per_minute=1000))
    return EnrollRelay(factory(fake, kwargs.pop("enrol_s", 2.0)), **kwargs)


def app_with(relay: EnrollRelay, extra: Mapping[str, EnrollRelay] | None = None) -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/enroll", relay.handle)
    for path, other in (extra or {}).items():
        app.router.add_route("*", path, other.handle)
    return app


async def send(client: TestClient, method: str = "POST", path: str = "/enroll", **kwargs: Any) -> Reply:
    if "data" in kwargs:
        kwargs.setdefault("headers", {"Content-Type": "application/json"})
    elif method == "POST":
        kwargs.setdefault("json", BODY)
    async with client.request(method, path, **kwargs) as response:
        return Reply(response.status, dict(response.headers), await response.read())


def exchange(app: web.Application, *requests: Mapping[str, Any]) -> list[Reply]:
    """Each request (``send`` keyword arguments) sent in order to a loopback ``TestServer`` of *app*."""

    async def main() -> list[Reply]:
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            return [await send(client, **request) for request in requests]

    return asyncio.run(main())


def post(relay: EnrollRelay, **kwargs: Any) -> Reply:
    return exchange(app_with(relay), kwargs)[0]


def mocked(method: str = "POST", peer: object = ("127.0.0.1", 1), body: bytes = b"", headers=None) -> web.Request:
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda name, default=None: peer if name == "peername" else default
    protocol = mock.Mock(_reading_paused=False)
    payload = streams.StreamReader(protocol, 2**16, loop=asyncio.get_running_loop())
    payload.feed_data(body)
    payload.feed_eof()
    hdrs = {**JSON, "Content-Length": str(len(body)), **(headers or {})}
    return make_mocked_request(method, "/enroll", headers=hdrs, transport=transport, payload=payload)


async def status_of(relay: EnrollRelay, request: web.Request) -> int:
    try:
        return (await relay.handle(request)).status
    except web.HTTPException as refused:
        return refused.status
