# SPDX-License-Identifier: Apache-2.0
"""WS upgrade authentication (T1.07a), adversarial inputs: odd header values, odd gate answers, failures of the
injected callables, concurrent mixed requests, and the rule that this module never imports the real gate."""

from __future__ import annotations

import ast
import asyncio
import base64
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from server_auth_helpers import (
    CERT_VALUES,
    FP,
    OTHER_FP,
    SpyRegistry,
    cert_header,
    creds,
    deps_for,
    enrolled,
    fetch,
    get,
    make_app,
    refusal,
)

from glasses.registry import Device
from glasses.server_auth import AuthDeps, authenticate

UNAUTHORIZED = (401, "unauthorized")
UNAVAILABLE = (503, "unavailable")
REVOKED = (403, "device_revoked")
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


class AlwaysEqual:
    """Equal to everything and truthy: passes a loose ``== True`` / ``if answer`` check, but is not ``True``."""

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash(True)

    def __bool__(self) -> bool:
        return True


class TruthyInt(int):
    """An int subclass with the value 1 (``bool`` itself cannot be subclassed)."""


def make_request(headers: dict[str, str]):
    return make_mocked_request("GET", "/ws", headers=headers)


def with_gate(gate, registry, revoked=lambda device_id: False) -> AuthDeps:
    return AuthDeps(gate_ok_async=gate, registry=registry, is_revoked=revoked)  # type: ignore[arg-type]


@pytest.mark.parametrize("answer", [AlwaysEqual(), TruthyInt(1), 1.0, b"\x01", object])
def test_a_gate_answer_that_merely_compares_or_tests_true_is_a_503(tmp_path, answer):
    async def odd() -> object:
        return answer

    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []
    assert refusal(get(make_app(with_gate(odd, registry), handled), creds(token)), *UNAVAILABLE)
    assert handled == []


def test_a_gate_that_never_answers_admits_nobody_and_reads_no_registry():
    """The bound on a stuck gate belongs to the gate (the adapter); this module must not skip past it."""
    spy = SpyRegistry(device=Device("d1", "n", FP, (), (), "t", None, None))

    async def hang() -> bool:
        await asyncio.sleep(3600)
        return True

    async def main() -> str:
        request = make_request(creds("t" * 43))
        try:
            await asyncio.wait_for(authenticate(request, with_gate(hang, spy), FP), 0.3)
        except TimeoutError:
            return "timeout"
        return "returned"

    assert asyncio.run(main()) == "timeout"
    assert spy.calls == []


def test_a_cancelled_gate_check_is_not_turned_into_an_admission():
    async def cancelled() -> bool:
        raise asyncio.CancelledError

    async def main() -> str:
        try:
            await authenticate(make_request(creds("t" * 43)), with_gate(cancelled, SpyRegistry()), FP)
        except asyncio.CancelledError:
            return "cancelled"
        except web.HTTPException:
            return "http"
        return "admitted"

    assert asyncio.run(main()) in ("cancelled", "http")


def test_header_names_are_case_insensitive_but_other_spellings_are_not_credentials(tmp_path):
    registry, device, token = enrolled(tmp_path)
    handled: list[str] = []
    app_for = lambda: make_app(deps_for(registry), handled)  # noqa: E731 - an app belongs to one event loop
    assert get(app_for(), {"authorization": f"Bearer {token}", "x-client-cert": CERT_VALUES[FP]}).status == 101
    for fp_name in ("X_Client_Cert", "X-Client-Cert-Fp", "X-Forwarded-Client-Cert", "X-Client-Cert-Pem"):
        reply = get(app_for(), {"Authorization": f"Bearer {token}", fp_name: CERT_VALUES[FP]})
        assert refusal(reply, *UNAUTHORIZED), fp_name
    assert handled == [device.device_id]


@pytest.mark.parametrize(
    ("scheme", "admitted"),
    [("Bearer", True), ("bearer", True), ("BEARER", True), ("bEaReR", True)]
    + [("Bearer\t", False), ("Bearer:", False), ("Bearerx", False), ("Ｂearer", False), ("Bearer\u00a0", False)],
)
def test_the_bearer_scheme_is_matched_case_insensitively_and_nothing_looser(tmp_path, scheme, admitted):
    registry, _device, token = enrolled(tmp_path)
    separator = "" if scheme.endswith(("\t", "\u00a0")) else " "
    reply = get(make_app(deps_for(registry), []), {"Authorization": f"{scheme}{separator}{token}", **cert_header()})
    assert (reply.status == 101) is admitted, reply
    if not admitted:
        assert refusal(reply, *UNAUTHORIZED)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        FP,
        "sha256:" + FP,
        CERT_VALUES[FP] + "%0A",
        CERT_VALUES[FP][:-12],
        CERT_VALUES[FP] + CERT_VALUES[OTHER_FP],
    ],
)
def test_a_certificate_header_that_is_not_exactly_one_certificate_is_401(tmp_path, value):
    registry, _device, token = enrolled(tmp_path)
    handled: list[str] = []
    reply = get(make_app(deps_for(registry), handled), {"Authorization": f"Bearer {token}", "X-Client-Cert": value})
    assert refusal(reply, *UNAUTHORIZED), reply
    assert handled == []


def test_the_token_is_case_sensitive(tmp_path):
    registry, _device, token = enrolled(tmp_path)
    assert get(make_app(deps_for(registry), []), creds(token)).status == 101
    assert refusal(get(make_app(deps_for(registry), []), creds(token.swapcase(), FP)), *UNAUTHORIZED)


def test_repeated_credentials_are_refused_in_every_spelling(tmp_path):
    """Raw requests: the aiohttp client merges differently cased repeats, a server must meet them as sent."""
    registry, _device, token = enrolled(tmp_path)
    auth, fp = f"Authorization: Bearer {token}".encode(), f"X-Client-Cert: {CERT_VALUES[FP]}".encode()
    sent = [
        [auth, fp],
        [auth, fp, fp],
        [auth, fp, b"x-client-cert: " + CERT_VALUES[OTHER_FP].encode()],
        [auth, fp, b"authorization: Bearer " + token.encode()],
        [auth, fp, b"AUTHORIZATION: Bearer x"],
        [fp, b"Authorization: Bearer x", auth],
    ]

    async def main() -> list[bytes]:
        async with TestClient(TestServer(make_app(deps_for(registry), []), host="127.0.0.1")) as client:
            loop = asyncio.get_running_loop()
            return [await loop.run_in_executor(None, raw_upgrade, client.port or 0, lines) for lines in sent]

    answers = asyncio.run(main())
    assert [a.split(b"\r\n", 1)[0] for a in answers] == [b"HTTP/1.1 101 Switching Protocols"] + [
        b"HTTP/1.1 401 Unauthorized"
    ] * 5


def raw_upgrade(port: int, header_lines: list[bytes]) -> bytes:
    request = b"GET /ws HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n"
    nonce = base64.b64encode(os.urandom(16))  # built at run time: a literal key trips the secret scan
    request += b"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + nonce + b"\r\n"
    request += b"".join(line + b"\r\n" for line in header_lines) + b"\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=3) as conn:
        conn.sendall(request)
        chunks = []
        try:
            while b"\r\n\r\n" not in b"".join(chunks) and (chunk := conn.recv(4096)):
                chunks.append(chunk)
        except TimeoutError:
            pass
    return b"".join(chunks)


@pytest.mark.parametrize(
    "bad",
    [
        b"Bearer \xff\xfe\xfd",
        b"Bearer " + b"\xc3\x28" * 20,
        b"Bearer \xed\xa0\x80",
        b"Bearer abc\x00def",
        b"Bearer " + b"\xf0\x9f\x98\x80",
    ],
)
def test_raw_non_utf8_or_control_bytes_in_a_credential_are_401_not_a_server_error(tmp_path, bad):
    registry, _device, token = enrolled(tmp_path)

    async def main() -> bytes:
        async with TestClient(TestServer(make_app(deps_for(registry), []), host="127.0.0.1")) as client:
            port = client.port or 0
            loop = asyncio.get_running_loop()
            lines = [b"Authorization: " + bad, b"X-Client-Cert: " + CERT_VALUES[FP].encode()]
            first = await loop.run_in_executor(None, raw_upgrade, port, lines)
            lines = [
                b"Authorization: Bearer " + token.encode(),
                b"X-Client-Cert: " + CERT_VALUES[FP].encode() + b"\xff",
            ]
            second = await loop.run_in_executor(None, raw_upgrade, port, lines)
            return first + b"\n=====\n" + second

    answer = asyncio.run(main())
    for part in answer.split(b"\n=====\n"):
        status = part.split(b"\r\n", 1)[0]
        assert status[:12] in (b"HTTP/1.1 401", b"HTTP/1.1 400", b"HTTP/1.0 400"), status  # never 101, 200 or 500
        assert b"Traceback" not in part
        assert token.encode() not in part


def test_the_revocation_check_failing_or_answering_oddly_never_admits(tmp_path):
    registry, _device, token = enrolled(tmp_path)

    def broken(device_id: str) -> bool:
        raise RuntimeError("revocation table unreadable")

    # A raising check and an odd answer are the fixed 503 (no wipe): never 101.
    for revoked, allowed in (
        (broken, {503}),
        (lambda device_id: 1, {503}),
        (lambda device_id: "x", {503}),
    ):
        handled: list[str] = []
        deps = AuthDeps(gate_ok_async=deps_for(registry).gate_ok_async, registry=registry, is_revoked=revoked)
        reply = get(make_app(deps, handled), creds(token))
        assert reply.status in allowed, reply
        assert handled == []
        assert token.encode() not in reply.body


def test_a_registry_failure_of_any_type_never_admits(tmp_path):
    """Any exception type of the registry is the fixed 401: it never opens the upgrade, never leaks."""
    for error in (ValueError("x"), UnicodeEncodeError("utf-8", "s", 0, 1, "surrogates"), KeyError("k"), MemoryError()):
        handled: list[str] = []
        reply = get(make_app(deps_for(SpyRegistry(error=error)), handled), creds("t" * 43))
        assert refusal(reply, *UNAUTHORIZED), reply
        assert handled == []
        assert b"Traceback" not in reply.body


def test_a_registry_lookup_failing_with_a_disk_error_is_401(tmp_path):
    class Broken:
        def lookup(self, token, fingerprint):
            raise OSError("disk")

    reply = get(make_app(deps_for(Broken()), []), creds("t" * 43))
    assert refusal(reply, *UNAUTHORIZED)


def test_revocation_without_credentials_is_not_revealed():
    """Everything revoked, no credentials: the answer is the plain 401, never the wipe marker."""
    spy = SpyRegistry(tombstone=True)
    reply = get(make_app(deps_for(spy, revoked=frozenset({"d1"})), []), {})
    assert refusal(reply, *UNAUTHORIZED)
    assert spy.calls == []


def test_a_registry_that_would_admit_anyone_is_never_asked_about_an_unusable_header_set():
    """The module itself refuses the header faults; it must not lean on the registry to notice them."""
    device = Device("d1", "n", FP, (), (), "t", None, None)
    token = "t" * 43
    faulty = [
        {"Authorization": f"Bearer {token}"},
        {"X-Client-Cert-Fp": FP},
        {"Authorization": f"Basic {token}", **cert_header()},
        {"Authorization": f"Bearer {token}" + "a" * 600, **cert_header()},
        {"Authorization": f"Bearer {token}", "X-Client-Cert": "a" * 600},
    ]
    for headers in faulty:
        spy = SpyRegistry(device=device)
        assert refusal(get(make_app(deps_for(spy), []), headers), *UNAUTHORIZED), headers
        assert spy.calls == [], headers


def test_forty_concurrent_mixed_requests_each_get_their_own_answer(tmp_path):
    registry, device, token = enrolled(tmp_path)
    second = registry.add("second", OTHER_FP, ["main"])[0]
    registry.revoke(second.device_id)
    handled: list[str] = []
    cases = {
        "ok": (creds(token), 101),
        "wrong-token": (creds("z" * 43), 401),
        "other-cert": (creds(token, OTHER_FP), 403),  # OTHER_FP is a tombstone now
        "no-headers": ({}, 401),
    }

    async def main() -> list[tuple[str, int]]:
        async with TestClient(TestServer(make_app(deps_for(registry), handled), host="127.0.0.1")) as client:
            names = list(cases) * 10

            async def one(name: str) -> tuple[str, int]:
                reply = await fetch(client, cases[name][0])
                return name, reply.status

            return await asyncio.gather(*(one(name) for name in names))

    results = asyncio.run(main())
    assert len(results) == 40
    assert all(status == cases[name][1] for name, status in results), results
    assert handled == [device.device_id] * 10


def imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(("." * node.level) + (node.module or ""))
            names.update(("." * node.level) + (node.module or "") + "." + alias.name for alias in node.names)
    return names


def test_server_auth_does_not_import_the_real_gate_statically_or_at_run_time():
    names = imported_modules(PLUGIN_ROOT / "glasses" / "server_auth.py")
    assert not {n for n in names if n.rstrip(".").endswith("gate") or ".gate" in n or n.endswith(".adapter")}, names
    probe = (
        "import sys; import glasses.server_auth; "
        "sys.exit(1 if {'glasses.gate', 'glasses.adapter'} & set(sys.modules) else 0)"
    )
    done = subprocess.run([sys.executable, "-c", probe], cwd=PLUGIN_ROOT, capture_output=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr.decode()
