# SPDX-License-Identifier: Apache-2.0
"""Edge and failure cases of the ``/enroll`` relay: the order of the refusals, bodies that lie about their size or
encoding, look-alike keys and non-string values, IPv6 peers, the exact 503 log line and 429/200 headers, and the
in-flight slots when a request is cancelled while its signer call is still running."""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import logging
import threading
from unittest import mock

import enroll_helpers
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from enroll_helpers import BODY, CODE, JSON, SIG, SPKI, app_with, mocked, post, relay_for, send, status_of
from fake_signer import Script

from glasses import enroll as enroll_module
from glasses import signer_client
from glasses.enroll import BODY_CAP, MAX_IN_FLIGHT, EnrollRelay, RateLimiter, is_loopback_peer

ESCAPED = json.dumps(BODY).replace('"spki"', '"\\u0073pki"')  # the key written as a JSON escape

fake = enroll_helpers.fake  # the shared fixture, bound here so pytest finds it


# Order of the refusals


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_a_non_loopback_peer_gets_404_not_405_for_any_method(fake, method):
    async def main() -> int:
        return await status_of(relay_for(fake), mocked(method, peer=("192.0.2.1", 1)))

    assert asyncio.run(main()) == 404
    assert fake.requests == []


def test_a_rate_limited_oversized_body_is_429_before_413(fake):
    relay = relay_for(fake, limiter=RateLimiter(per_minute=0))
    reply = post(relay, data=b"x" * (BODY_CAP + 10))
    assert reply.status == 429
    assert fake.requests == []


def test_a_wrong_content_type_is_400_before_the_size_check(fake):
    reply = post(relay_for(fake), data=b"x" * (BODY_CAP + 10), headers={"Content-Type": "text/plain"})
    assert reply.status == 400


# Bodies that lie


def test_a_small_content_length_with_a_big_body_reads_only_the_declared_bytes(fake):
    async def main() -> bytes:
        async with TestServer(app_with(relay_for(fake)), host="127.0.0.1") as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            head = b"POST /enroll HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n"
            writer.write(head + b"{}" + b"A" * (BODY_CAP * 4))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(65536), 5)
            writer.close()
            return data

    data = asyncio.run(main())
    assert data.split(b"\r\n", 1)[0].split(b" ")[1] == b"400"
    assert fake.requests == []


@pytest.mark.parametrize(
    "data",
    [
        b'{"spki": "\xff\xfe", "code": "c", "signature": "s"}',
        b"\xef\xbb\xbf" + b'{"spki": "a", "code": "b", "signature": "c"}',
        '{"spki": "a", "code": "b", "signature": "c"}'.encode("utf-16"),
        b'{"spki": "a", "code": "b", "signature": "c"}\x00',
    ],
    ids=["invalid-utf8", "utf8-bom", "utf16", "trailing-nul"],
)
def test_a_body_that_is_not_plain_utf8_json_is_400(fake, data):
    reply = post(relay_for(fake), data=data, headers={"Content-Type": "application/json; charset=utf-16"})
    assert reply.status == 400
    assert fake.requests == []


# Values and keys


@pytest.mark.parametrize("value", [1, None, True, [], {}, 1.5, ""])
@pytest.mark.parametrize("key", ["spki", "code", "signature"])
def test_a_non_string_or_empty_value_is_400_without_a_signer_call(fake, key, value):
    reply = post(relay_for(fake), json={**BODY, key: value})
    assert reply.status == 400
    assert fake.requests == []


@pytest.mark.parametrize(
    "key", ["\u0455pki", "SPKI", "spki ", "spki\u200b", "sig", "signature\x00", "c\u043ede"], ids=repr
)
def test_a_look_alike_key_is_400_without_a_signer_call(fake, key):
    good = "spki" if key.lower().startswith(("spki", "\u0455")) else "code" if "de" in key else "signature"
    body = {k: v for k, v in BODY.items() if k != good}
    body[key] = BODY[good]
    reply = post(relay_for(fake), json=body)
    assert reply.status == 400
    assert fake.requests == []


def test_an_escaped_duplicate_key_is_400(fake):
    data = ('{"spki": "' + SPKI + '", ' + ESCAPED[1:]).encode()
    reply = post(relay_for(fake), data=data)
    assert reply.status == 400
    assert fake.requests == []


def test_an_escaped_key_spelling_the_same_name_is_the_same_key(fake):
    assert ESCAPED.startswith('{"\\u0073pki"') and CODE in ESCAPED and SIG in ESCAPED
    assert post(relay_for(fake), data=ESCAPED.encode()).status == 200


# Peers


@pytest.mark.parametrize(
    ("peername", "allowed"),
    [
        (("::1", 1, 0, 0), True),
        (("0:0:0:0:0:0:0:1", 1, 0, 0), True),
        (("fe80::1%lo", 1, 0, 1), False),
        (("::", 1, 0, 0), False),
        (("0.0.0.0", 1), False),
        (("127.0.0.1 ", 1), False),
        ((b"127.0.0.1", 1), False),
    ],
    ids=repr,
)
def test_ipv6_and_odd_peers(peername, allowed):
    async def main() -> bool:
        return is_loopback_peer(mocked(peer=peername))

    assert asyncio.run(main()) is allowed


def test_an_ipv4_mapped_peer_follows_the_runtime_ipaddress_rule():
    # Python >= 3.13 treats ::ffff:127.0.0.1 as loopback, 3.11/3.12 do not (refused there: fail-closed).
    async def main() -> bool:
        return is_loopback_peer(mocked(peer=("::ffff:127.0.0.1", 1, 0, 0)))

    assert asyncio.run(main()) is ipaddress.ip_address("::ffff:127.0.0.1").is_loopback


# Exact answers


def test_the_503_log_line_names_only_the_error_kind(fake, caplog):
    fake.scripts["enrol"] = Script(error="internal")
    with caplog.at_level(logging.DEBUG, logger="glasses.enroll"):
        reply = post(relay_for(fake))
    assert reply.status == 503
    records = [r for r in caplog.records if r.name == "glasses.enroll"]
    assert [r.getMessage() for r in records] == ["enrol relay: signer call failed (SignerRefusal)"]
    assert records[0].levelno == logging.WARNING and records[0].exc_info is None


def test_a_rate_limited_answer_never_echoes_retry_after(fake):
    fake.scripts["enrol"] = Script(error="rate_limited", extras={"retry_after_s": 4242})
    reply = post(relay_for(fake))
    assert reply.status == 429
    assert "Retry-After" not in reply.headers
    assert b"4242" not in reply.body


def test_the_success_answer_carries_no_extra_headers(fake):
    reply = post(relay_for(fake))
    assert reply.status == 200
    names = {name.lower() for name in reply.headers}
    assert names <= {"content-type", "content-length", "cache-control", "date", "server"}


def test_the_413_body_is_fixed_text(fake):
    reply = post(relay_for(fake), data=b"y" * (BODY_CAP + 1))
    assert (reply.status, reply.body) == (413, b"413: Request Entity Too Large")


@pytest.mark.parametrize("declared", [True, False], ids=["content_length", "no_length"])
def test_an_oversized_body_is_refused_without_reading_past_the_cap(fake, declared):
    total = 200_000

    async def main() -> tuple[int, int]:
        request = mocked(body=b" " * total)
        if not declared:
            request = request.clone(headers={**JSON})
        status = await status_of(relay_for(fake), request)
        return status, len(await request.content.read())

    status, unread = asyncio.run(main())
    assert status == 413
    assert unread == total if declared else total - unread <= BODY_CAP + 1
    assert fake.requests == []


def test_a_json_parser_recursion_error_is_400_not_500(fake, monkeypatch):
    # 3.12+ parse the deepest nesting a 4 KiB body holds; 3.11, the floor, raises RecursionError on it (measured).
    def overflow(*_args: object, **_kwargs: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(enroll_module.json, "loads", overflow)
    reply = post(relay_for(fake), data=json.dumps(BODY).encode())
    assert reply.status == 400
    assert fake.requests == []


def test_an_unexpected_client_error_is_a_503_naming_only_its_type_and_frees_its_slot(caplog):
    def broken() -> object:
        raise RuntimeError("boom " + CODE)

    relay = EnrollRelay(broken, limiter=RateLimiter(per_minute=1000))  # type: ignore[arg-type]
    with caplog.at_level(logging.DEBUG):
        replies = [post(relay) for _ in range(MAX_IN_FLIGHT + 1)]
    assert [r.status for r in replies] == [503] * (MAX_IN_FLIGHT + 1)
    assert all(CODE.encode() not in r.body for r in replies)
    assert CODE not in caplog.text and "boom" not in caplog.text
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == [
        "enrol relay: signer call failed (RuntimeError)"
    ] * (MAX_IN_FLIGHT + 1)
    assert relay._in_flight == 0


@pytest.mark.parametrize("error", [RuntimeError("boom"), signer_client.SignerUnavailable()], ids=repr)
def test_the_outcome_of_a_failed_call_carries_no_traceback(monkeypatch, error):
    def failing() -> object:
        raise error

    seen: list[object] = []
    monkeypatch.setattr(enroll_module, "_answer", lambda outcome: seen.append(outcome) or web.Response(status=503))
    assert post(EnrollRelay(failing, limiter=RateLimiter(per_minute=1000))).status == 503  # type: ignore[arg-type]
    assert seen == [error]
    assert error.__traceback__ is None


# In-flight slots and cancellation


def test_a_cancelled_request_keeps_its_slot_until_the_signer_call_ends(fake):
    fake.scripts["enrol"] = Script(delay_s=1.0)

    async def main() -> tuple[int, int]:
        async with TestClient(TestServer(app_with(relay_for(fake)), host="127.0.0.1")) as client:
            held = [asyncio.create_task(send(client)) for _ in range(MAX_IN_FLIGHT)]
            for _ in range(500):
                if len(fake.requests) >= MAX_IN_FLIGHT:
                    break
                await asyncio.sleep(0.01)
            for task in held:
                task.cancel()  # the clients go away while the signer still holds both enrols
            await asyncio.sleep(0.2)
            while_held = (await send(client)).status
            await asyncio.gather(*held, return_exceptions=True)
            await asyncio.sleep(1.2)
            after = (await send(client)).status
            return while_held, after

    while_held, after = asyncio.run(main())
    assert while_held == 429
    assert after == 200


def test_a_request_cancelled_before_its_thread_starts_frees_its_slot(fake):
    async def main() -> tuple[int, int]:
        gate = threading.Event()
        relay = relay_for(fake)
        busy = [relay._executor.submit(gate.wait, 5) for _ in range(MAX_IN_FLIGHT)]  # every relay worker is taken
        body = json.dumps(BODY).encode()
        tasks = [asyncio.create_task(status_of(relay, mocked(body=body))) for _ in range(MAX_IN_FLIGHT)]
        await asyncio.sleep(0.1)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        gate.set()
        concurrent.futures.wait(busy)
        await asyncio.sleep(0.1)
        ran = len(fake.requests)  # 0: the cancelled calls never started; their requests freed the slots
        return ran, await status_of(relay, mocked(body=body))

    assert asyncio.run(main()) == (0, 200)


def test_a_worker_whose_request_already_freed_the_slot_makes_no_call():
    claim = threading.Lock()
    claim.acquire()  # the cancelled request claimed it first
    relay = EnrollRelay(lambda: pytest.fail("no signer call"), limiter=RateLimiter(per_minute=1000))
    loop = mock.Mock()
    with pytest.raises(asyncio.CancelledError):
        relay._enrol(loop, (SPKI, CODE, SIG), claim)
    loop.call_soon_threadsafe.assert_not_called()


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_a_base_exception_from_the_signer_call_is_not_swallowed_and_the_slot_is_freed(error):
    """Only ``Exception`` becomes the 503 outcome; a shutdown signal propagates and the slot is still freed."""

    def interrupted() -> object:
        raise error()

    relay = EnrollRelay(interrupted, limiter=RateLimiter(per_minute=1000))  # type: ignore[arg-type]
    loop = mock.Mock()
    with pytest.raises(error):
        relay._enrol(loop, (SPKI, CODE, SIG), threading.Lock())
    loop.call_soon_threadsafe.assert_called_once_with(relay._release)
