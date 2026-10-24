# SPDX-License-Identifier: Apache-2.0
"""WS server ``/enroll`` mount (T1.07c): one ``EnrollRelay`` per app, routed for every method so the peer check
precedes the 405, never a WebSocket upgrade, closed on the loop thread in ``on_cleanup``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable
from typing import Any

import aiohttp
import enroll_helpers
import pytest
from enroll_helpers import BODY, factory
from fake_signer import FakeSigner, Script
from server_helpers import Wiring, app_for, serving, wiring

from glasses import server
from glasses.enroll import EnrollRelay

fake = enroll_helpers.fake


def run(coro: Awaitable[Any]) -> Any:
    return asyncio.run(asyncio.wait_for(coro, 30))  # type: ignore[arg-type]


def call(
    w: Wiring, fake_signer: FakeSigner | None, requests: list[dict[str, Any]], **changes: Any
) -> list[tuple[int, bytes]]:
    """Each request (``ClientSession.request`` keyword arguments, ``method`` and ``path`` included) in order."""
    if fake_signer is not None:
        changes.setdefault("signer_client", factory(fake_signer))

    async def main() -> list[tuple[int, bytes]]:
        replies = []
        async with serving(app_for(w, **changes)) as port, aiohttp.ClientSession() as session:
            for request in requests:
                options = dict(request)
                url = f"http://127.0.0.1:{port}{options.pop('path', '/enroll')}"
                async with session.request(options.pop("method", "POST"), url, **options) as response:
                    replies.append((response.status, await response.read()))
        return replies

    return run(main())  # type: ignore[no-any-return]


@pytest.mark.parametrize("method", ["GET", "PUT", "POST", "DELETE"])
def test_a_refused_peer_gets_the_unknown_path_404_for_every_method_and_reaches_no_signer(tmp_path, fake, method):
    w = wiring(tmp_path)
    w.allow_peer = False
    sent = {"method": method, "json": BODY} if method in ("POST", "PUT") else {"method": method}
    enroll, unknown = call(w, fake, [sent, {"method": method, "path": "/nope"}])
    assert enroll[0] == 404 and enroll == unknown  # the same status and body as a path that does not exist
    assert fake.requests == []


def test_a_peer_predicate_that_raises_is_the_same_404(tmp_path, fake):
    w = wiring(tmp_path)

    def broken(request: object) -> bool:
        raise RuntimeError("peer table unreadable")

    (reply,) = call(w, fake, [{"json": BODY}], peer_allowed=broken)
    assert reply[0] == 404 and fake.requests == []


def test_an_allowed_peer_reaches_the_signer_and_enrol_refused_is_the_unknown_path_404(tmp_path, fake):
    w = wiring(tmp_path)
    fake.scripts["enrol"] = Script(error="enrol_refused")
    enroll, unknown = call(w, fake, [{"json": BODY}, {"path": "/nope", "json": BODY}])
    assert enroll == unknown and enroll[0] == 404
    assert fake.ops == ["enrol"]


def test_an_allowed_peer_gets_the_signers_answer_with_no_store(tmp_path, fake):
    w = wiring(tmp_path)

    async def main() -> tuple[int, str | None, dict[str, Any]]:
        app = app_for(w, signer_client=factory(fake))
        async with serving(app) as port, aiohttp.ClientSession() as session:
            url = f"http://127.0.0.1:{port}/enroll"
            async with session.post(url, json=BODY) as response:
                return response.status, response.headers.get("Cache-Control"), await response.json()

    status, cache, body = run(main())
    assert status == 200 and cache == "no-store" and set(body) == {"cert", "token", "deviceId"}


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
def test_an_allowed_peer_gets_405_for_other_methods_from_the_relay_and_no_signer_call(tmp_path, fake, method):
    ((status, _),) = call(wiring(tmp_path), fake, [{"method": method}])
    assert status == 405 and fake.requests == []


def test_a_websocket_upgrade_on_enroll_is_refused(tmp_path, fake):
    async def attempt(w: Wiring) -> int:
        async with serving(app_for(w, signer_client=factory(fake))) as port, aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await session.ws_connect(f"http://127.0.0.1:{port}/enroll")
            return refused.value.status

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    allowed, denied = wiring(tmp_path / "a"), wiring(tmp_path / "b")
    denied.allow_peer = False
    assert (run(attempt(allowed)), run(attempt(denied))) == (405, 404)  # no 101 either way
    assert fake.requests == []


def test_a_body_over_4_kib_is_413_with_a_length_and_chunked_and_reaches_no_signer(tmp_path, fake):
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(5):
            yield b" " * 1024

    declared = {"data": b"{" + b" " * 5000 + b"}", "headers": {"Content-Type": "application/json"}}
    chunked = {"data": chunks(), "headers": {"Content-Type": "application/json"}}
    first, second = call(wiring(tmp_path), fake, [declared, chunked])
    assert first[0] == 413 and second[0] == 413 and fake.requests == []


def test_an_app_without_a_signer_client_answers_503_and_names_nothing(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    ((status, body),) = call(wiring(tmp_path), None, [{"json": BODY}])
    assert status == 503 and b"signer" not in body.lower() and "NotConfigured" in caplog.text
    assert BODY["code"] not in caplog.text and "Traceback" not in caplog.text


class Counting(EnrollRelay):
    built: list[Counting] = []
    closed_on_loop: list[bool] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        Counting.built.append(self)

    def close(self) -> None:
        try:
            asyncio.get_running_loop()
            Counting.closed_on_loop.append(True)
        except RuntimeError:
            Counting.closed_on_loop.append(False)
        super().close()


def test_the_app_builds_exactly_one_relay_mounts_it_for_every_method_and_closes_it_on_the_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EnrollRelay", Counting)
    monkeypatch.setattr(Counting, "built", [])
    monkeypatch.setattr(Counting, "closed_on_loop", [])
    app = app_for(wiring(tmp_path))
    (relay,) = Counting.built
    routes = [route for route in app.router.routes() if route.resource and route.resource.canonical == "/enroll"]
    assert [(route.method, route.handler.__self__) for route in routes] == [("*", relay)]  # type: ignore[attr-defined]

    async def main() -> None:
        async with serving(app):
            assert Counting.closed_on_loop == []

    run(main())
    assert Counting.closed_on_loop == [True] and len(Counting.built) == 1


def test_the_relay_gets_the_apps_peer_predicate_as_its_peer_check(tmp_path, fake):
    w = wiring(tmp_path)
    w.allow_peer = False
    call(w, fake, [{"json": BODY}])
    assert w.peers == ["/enroll"]  # the one predicate of the app saw the /enroll request


def test_two_apps_have_two_relays_and_neither_shares_the_rate_limit(tmp_path, fake):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    statuses = [status for status, _ in call(wiring(tmp_path / "a"), fake, [{"json": BODY}] * 7)]
    assert statuses.count(429) == 1  # the relay's global 6 a minute: the 7th of one app
    assert call(wiring(tmp_path / "b"), fake, [{"json": BODY}])[0][0] == 200
