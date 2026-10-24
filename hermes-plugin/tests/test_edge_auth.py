# SPDX-License-Identifier: Apache-2.0
"""The edge-peer check (``glasses.edge_auth``): ``edge.user`` resolution, the ``SO_PEERCRED`` uid of a real unix
connection, and the request predicate in front of ``/ws`` and ``/enroll`` over the real listener: it runs before
``authenticate`` (S3), the forwarded fingerprint header counts only from the allowed peer, a refused peer reaches no
registry, gate, ``is_revoked`` or signer (SAT-SC-11 (1), N1), a token with another fingerprint gets ``401``
(SAT-CA-55), one registry read per failing request, and a request the parser refuses leaks no credential (N1)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pwd
import socket
from pathlib import Path
from types import SimpleNamespace

import enroll_helpers
import pytest
from edge_helpers import SpyRegistry, edge_serving, other_user, raw_unix, request_bytes, short_dir
from enroll_helpers import BODY, factory
from server_auth_helpers import OTHER_FP, cert_header
from server_helpers import app_for, wiring

from glasses import edge_auth
from glasses.edge_auth import EdgeUserRefused, edge_peer_allowed, edge_uid, peer_uid

fake = enroll_helpers.fake
ME = os.geteuid()


@pytest.mark.parametrize("name", ["", "no-such-user-x9", "root", pwd.getpwuid(ME).pw_name])
def test_an_unset_unknown_root_or_own_edge_user_is_a_start_error(name: str) -> None:
    with pytest.raises(EdgeUserRefused):
        edge_uid(name)


def test_another_real_user_resolves_to_its_uid() -> None:
    user = other_user()
    assert edge_uid(user.pw_name) == user.pw_uid


def test_the_peer_uid_of_a_real_unix_connection_is_read_with_so_peercred() -> None:
    left, right = socket.socketpair(socket.AF_UNIX)
    with left, right:
        assert peer_uid(SimpleNamespace(get_extra_info=lambda key: left if key == "socket" else None)) == ME
    with pytest.raises(OSError):
        peer_uid(SimpleNamespace(get_extra_info=lambda _key: None))


def test_a_request_without_a_transport_is_refused() -> None:
    assert edge_peer_allowed(ME)(SimpleNamespace(transport=None)) is False


def _spied(tmp_path: Path, uid: int, **changes: object):
    w = wiring(tmp_path)
    spy = SpyRegistry(w.registry)
    calls: list[str] = []

    async def gate() -> bool:
        calls.append("gate")
        return True

    def revoked(_id: str) -> bool:
        calls.append("is_revoked")
        return False

    values = {"registry": spy, "gate_ok_async": gate, "is_revoked": revoked, "peer_allowed": edge_peer_allowed(uid)}
    return w, spy, calls, app_for(w, **{**values, **changes})


async def _send(app: object, data: bytes, wait: float = 0.5) -> bytes:
    with short_dir() as d:
        async with edge_serving(app, d / "edge.sock", ME):  # the listener admits this process; the predicate decides
            return await raw_unix(d / "edge.sock", data, wait)


def test_the_real_predicate_runs_before_authenticate_and_a_forged_header_counts_only_from_the_edge_peer(
    tmp_path: Path,
) -> None:
    w, spy, calls, refused = _spied(tmp_path, ME + 1)
    reply = asyncio.run(_send(refused, request_bytes("GET", "/ws", w.headers())))
    assert reply.startswith(b"HTTP/1.1 404") and (spy.lookups, calls) == ([], [])
    (tmp_path / "b").mkdir()
    w, spy, calls, allowed = _spied(tmp_path / "b", ME)
    reply = asyncio.run(_send(allowed, request_bytes("GET", "/ws", w.headers())))
    assert reply.startswith(b"HTTP/1.1 101") and len(spy.lookups) == 1 and "gate" in calls


def test_a_token_with_another_fingerprint_gets_401_over_the_unix_socket(tmp_path: Path) -> None:
    w, spy, _calls, app = _spied(tmp_path, ME)
    headers = {**w.headers(), **cert_header(OTHER_FP)}
    reply = asyncio.run(_send(app, request_bytes("GET", "/ws", headers)))
    assert reply.startswith(b"HTTP/1.1 401") and len(spy.lookups) == 1  # one registry read per failing request


@pytest.mark.parametrize("method", ["GET", "PUT", "POST"])
def test_a_refused_peer_on_enroll_gets_404_and_the_signer_receives_nothing(tmp_path: Path, fake, method: str) -> None:
    w, _spy, calls, app = _spied(tmp_path, ME + 1, signer_client=factory(fake))
    body = json.dumps(BODY).encode()
    data = request_bytes(method, "/enroll", {"Content-Type": "application/json"}, body)
    assert asyncio.run(_send(app, data)).startswith(b"HTTP/1.1 404")
    assert (fake.ops, calls) == ([], [])


def test_the_edge_peer_reaches_the_signer_on_enroll(tmp_path: Path, fake) -> None:
    _w, _spy, _calls, app = _spied(tmp_path, ME, signer_client=factory(fake))
    data = request_bytes("POST", "/enroll", {"Content-Type": "application/json"}, json.dumps(BODY).encode())
    asyncio.run(_send(app, data, wait=3.0))
    assert fake.ops == ["enrol"]


@pytest.mark.parametrize("bad", [b"\x01", b"\x7f", b"x" * 9000, b"\r\n folded"])
def test_a_refused_request_line_leaks_neither_token_nor_fingerprint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad: bytes
) -> None:
    w, _spy, _calls, app = _spied(tmp_path, ME)
    token, fp = w.token.encode(), w.headers()["X-Client-Cert"].encode()
    data = b"GET /ws HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " + token + bad + b"\r\nX-Client-Cert: " + fp
    with caplog.at_level(logging.DEBUG):
        reply = asyncio.run(_send(app, data + b"\r\n\r\n"))
    assert reply.split(b" ")[1] == b"400"
    for secret in (token, fp):
        assert secret not in reply and secret.decode() not in caplog.text


def test_unreadable_credentials_in_the_predicate_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_transport: object) -> int:
        raise OSError("no credentials")

    monkeypatch.setattr(edge_auth, "peer_uid", broken)
    with pytest.raises(OSError):
        edge_peer_allowed(ME)(SimpleNamespace(transport=object()))
