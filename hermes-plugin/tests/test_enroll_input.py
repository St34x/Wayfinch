# SPDX-License-Identifier: Apache-2.0
"""The ``/enroll`` relay refuses before any signer call: oversized, malformed or unexpected bodies (SAT-CA-37, 38),
a peer that is not loopback (SAT-CA-44), the rate limit (SAT-CA-40, HTTP half) and a third concurrent relay; a held
``enrol`` never blocks the event loop."""

from __future__ import annotations

import ast
import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest import mock

import enroll_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from enroll_helpers import BODY, app_with, exchange, post, relay_for, send
from fake_signer import Script

from glasses import enroll
from glasses.enroll import BODY_CAP, MAX_IN_FLIGHT, EnrollRelay, RateLimiter, is_loopback_peer

JSON = {"Content-Type": "application/json"}

fake = enroll_helpers.fake  # the shared fixture, bound here so pytest finds it


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), 1000):
        yield data[start : start + 1000]


def padded(size: int) -> bytes:
    raw = json.dumps(BODY).encode()
    return raw + b" " * (size - len(raw))


# Body size (SAT-CA-38)


def test_a_body_of_exactly_the_cap_is_relayed(fake):
    assert post(relay_for(fake), data=padded(BODY_CAP)).status == 200
    assert fake.ops == ["enrol"]


@pytest.mark.parametrize("chunked", [False, True], ids=["content_length", "chunked"])
def test_a_body_over_the_cap_is_413_without_a_signer_call(fake, chunked):
    data = padded(BODY_CAP + 1)
    reply = post(relay_for(fake), data=chunks(data) if chunked else data, headers=JSON)
    assert reply.status == 413
    assert fake.requests == []


def test_a_large_chunked_body_is_413_and_not_read_to_the_end(fake):
    reply = post(relay_for(fake), data=chunks(b" " * 200_000), headers=JSON)
    assert reply.status == 413
    assert fake.requests == []


def test_a_lying_content_length_is_413_at_once_without_waiting_for_the_body(fake):
    async def main() -> bytes:
        async with TestServer(app_with(relay_for(fake)), host="127.0.0.1") as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            head = (
                b"POST /enroll HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 999999\r\n\r\n"
            )
            writer.write(head + b"{}")  # the rest of the declared body never comes
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)
            writer.close()
            return status_line

    assert asyncio.run(main()).split(b" ")[1] == b"413"
    assert fake.requests == []


# Malformed and unexpected bodies (SAT-CA-37, SAT-CA-38)


@pytest.mark.parametrize("key", ["alg", "lanes", "name", "deviceId", "csr", "ca"])
def test_an_extra_key_is_400_without_a_signer_call(fake, key):
    assert post(relay_for(fake), json={**BODY, key: "x"}).status == 400
    assert fake.requests == []


BAD_BODIES = {
    "not_json": b"hello",
    "array": json.dumps([BODY]).encode(),
    "nested": json.dumps({**BODY, "spki": {"a": 1}}).encode(),
    "number": json.dumps({**BODY, "code": 7}).encode(),
    "null": json.dumps({**BODY, "signature": None}).encode(),
    "deep": b"[" * BODY_CAP,
    "deep_object": b'{"a":' * 800,
    "duplicate": b'{"spki": "x", ' + json.dumps(BODY).encode()[1:],
    "nan": b'{"spki": NaN, "code": "x", "signature": "y"}',
    "infinity": b'{"spki": "x", "code": Infinity, "signature": "y"}',
    "missing": json.dumps({"spki": BODY["spki"], "code": BODY["code"]}).encode(),
    "not_utf8": b'{"spki": "\xff", "code": "x", "signature": "y"}',
    "empty": b"",
}


@pytest.mark.parametrize("data", BAD_BODIES.values(), ids=BAD_BODIES.keys())
def test_a_malformed_body_is_400_never_500(fake, data):
    made: list[int] = []
    client = enroll_helpers.factory(fake)
    relay = EnrollRelay(lambda: made.append(1) or client(), limiter=RateLimiter(per_minute=1000))
    assert post(relay, data=data).status == 400
    assert made == []  # refused by the relay itself: the "number"/"null" cases stop at its exact-str check
    assert fake.requests == []


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded", "application/json5"])
def test_a_wrong_content_type_is_400(fake, content_type):
    reply = post(relay_for(fake), data=json.dumps(BODY).encode(), headers={"Content-Type": content_type})
    assert reply.status == 400
    assert fake.requests == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
def test_a_method_other_than_post_is_405(fake, method):
    reply = exchange(app_with(relay_for(fake)), {"method": method})[0]
    assert (reply.status, reply.headers.get("Allow")) == (405, "POST")
    assert fake.requests == []


# Peer check (SAT-CA-44, SAT-SC-05)


def peer_request(peername: object, headers: dict[str, str] | None = None) -> web.Request:
    transport = mock.Mock()
    transport.get_extra_info.side_effect = lambda name, default=None: peername if name == "peername" else default
    return make_mocked_request("POST", "/enroll", headers=headers, transport=transport)


async def handled(relay: EnrollRelay, request: web.Request) -> int:
    try:
        return (await relay.handle(request)).status
    except web.HTTPException as refused:
        return refused.status


@pytest.mark.parametrize(
    "headers", [None, {"X-Forwarded-For": "127.0.0.1"}, {"X-Client-Cert": "planted", "X-Real-IP": "127.0.0.1"}]
)
def test_a_test_net_peer_is_404_whatever_its_headers_say(fake, headers):
    request = peer_request(("192.0.2.1", 40000), headers)
    assert asyncio.run(handled(relay_for(fake), request)) == 404
    assert fake.requests == []


@pytest.mark.parametrize(
    ("peername", "allowed"),
    [
        (("127.0.0.1", 1), True),
        (("127.8.9.10", 1), True),
        (("::1", 1, 0, 0), True),
        (("192.0.2.1", 1), False),
        (("::ffff:192.0.2.1", 1, 0, 0), False),
        (("10.0.0.1", 1), False),
        ("", False),
        ("/run/edge.sock", False),
        (None, False),
        (("not an address", 1), False),
        ((), False),
        ((7, 1), False),
    ],
)
def test_only_a_loopback_ip_peer_is_allowed(peername, allowed):
    assert is_loopback_peer(peer_request(peername)) is allowed


def test_a_request_without_a_transport_is_not_loopback():
    request = make_mocked_request("POST", "/enroll")
    with mock.patch.object(type(request), "transport", new_callable=mock.PropertyMock, return_value=None):
        assert is_loopback_peer(request) is False


# Rate limit (SAT-CA-40, HTTP half)


def test_the_seventh_request_in_a_minute_is_429_without_a_signer_call(fake):
    now = [1000.0]
    relay = relay_for(fake, limiter=RateLimiter(clock=lambda: now[0]))
    statuses = [r.status for r in exchange(app_with(relay), *[{}] * 7)]
    assert statuses == [200] * 6 + [429]
    assert fake.ops.count("enrol") == 6
    now[0] += 60.0
    assert post(relay).status == 200


def test_the_limiter_window_slides():
    now = [0.0]
    limiter = RateLimiter(per_minute=2, clock=lambda: now[0])
    assert [limiter.allow(), limiter.allow(), limiter.allow()] == [True, True, False]
    now[0] = 59.9
    assert limiter.allow() is False
    now[0] = 60.0
    assert [limiter.allow(), limiter.allow(), limiter.allow()] == [True, True, False]


def test_refusals_before_the_limiter_do_not_count(fake):
    relay = relay_for(fake, limiter=RateLimiter(per_minute=1))
    statuses = [r.status for r in exchange(app_with(relay), {"method": "GET"}, {}, {})]
    assert statuses == [405, 200, 429]
    assert fake.ops == ["enrol"]


# Concurrency and the event loop


def test_a_held_enrol_never_blocks_the_loop_and_a_third_relay_is_429(fake):
    fake.scripts["enrol"] = Script(delay_s=1.0)

    async def main() -> tuple[list[int], list[int], float]:
        async with TestClient(TestServer(app_with(relay_for(fake)), host="127.0.0.1")) as client:
            held = [asyncio.create_task(send(client)) for _ in range(MAX_IN_FLIGHT)]
            for _ in range(500):
                if len(fake.requests) >= MAX_IN_FLIGHT:
                    break
                await asyncio.sleep(0.01)
            start = time.monotonic()
            quick = [(await send(client)).status, (await send(client, method="GET")).status]
            elapsed = time.monotonic() - start
            return [(await task).status for task in held], quick, elapsed

    held, quick, elapsed = asyncio.run(main())
    assert held == [200] * MAX_IN_FLIGHT
    assert quick == [429, 405]
    assert elapsed < 0.5
    assert fake.ops.count("enrol") == MAX_IN_FLIGHT


def test_a_slot_is_free_again_once_the_relay_ends(fake):
    statuses = [r.status for r in exchange(app_with(relay_for(fake)), *[{}] * (MAX_IN_FLIGHT + 2))]
    assert statuses == [200] * (MAX_IN_FLIGHT + 2)


def test_the_relay_registers_no_route():
    tree = ast.parse(Path(enroll.__file__).read_text(encoding="utf-8"))
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    used |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    routing = {"router", "add_route", "add_routes", "add_post", "add_get", "add_view", "add_subapp"}
    assert not used & (routing | {"Application", "UrlDispatcher", "RouteTableDef", "AppRunner"})


def test_the_default_limiter_allows_six_per_minute():
    limiter = RateLimiter()
    assert [limiter.allow() for _ in range(7)] == [True] * 6 + [False]
