# SPDX-License-Identifier: Apache-2.0
"""The forwarded certificate through the server and the real unix listener (T1.184c; SAT-SC-12 and the adversarial
cases): from the edge peer, every refused certificate is the fixed ``401`` and the registry reader records no call; a
certificate of either CA of a two-CA bundle passes on to the token and fingerprint check in both encodings; the
verified fingerprint wins over a forged ``X-Client-Cert-Fp``; a repeated certificate header is refused; no
certificate bytes, token or fingerprint appear in a response or a log line."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import pytest
from cert_helpers import HEADER, NOW, der_base64, device_cert, encoded, fingerprint, make_ca, self_signed
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
from edge_helpers import SpyRegistry, edge_serving, raw_unix, short_dir
from server_auth_helpers import upgrade_headers
from server_helpers import app_for, wiring

from glasses.edge_auth import edge_peer_allowed
from glasses.edge_cert import EdgeCert

CA_A = make_ca("glasses device CA aaaa")
CA_B = make_ca("glasses device CA bbbb")
TOKEN = "S3ntinelTokenAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
ME = os.geteuid()

REFUSED_CERTS = {
    "another-ca": lambda: device_cert(make_ca("glasses device CA cccc")),
    "self-signed": self_signed,
    "expired": lambda: device_cert(CA_A, start=NOW - timedelta(days=91)),
    "not-yet-valid": lambda: device_cert(CA_A, start=NOW + timedelta(hours=1)),
    "ca-true": lambda: device_cert(CA_A, is_ca=True),
    "no-basic-constraints": lambda: device_cert(CA_A, basic_constraints=False),
    "eku-without-client-auth": lambda: device_cert(CA_A, eku=(ExtendedKeyUsageOID.SERVER_AUTH,)),
    "forged-issuer-name": lambda: device_cert(CA_A, signer=ec.generate_private_key(ec.SECP256R1())),
    "mismatched-aki": lambda: device_cert(CA_A, aki=b"\x02" * 20),
}


def _garbage() -> dict[str, list[tuple[str, str]]]:
    good = encoded(device_cert(CA_A), "pem-urlescaped")
    return {
        "garbage": [(HEADER, "not a certificate")],
        "empty": [(HEADER, "")],
        "over-the-cap": [(HEADER, "A" * 8193)],
        "trailing-data": [(HEADER, good + "junk")],
        "chain-of-two": [(HEADER, good + encoded(device_cert(CA_B), "pem-urlescaped"))],
        "twice": [(HEADER, good), (HEADER, good)],
        "fingerprint-header-only": [("X-Client-Cert-Fp", fingerprint(device_cert(CA_A)))],
    }


async def _exchange(
    tmp_path: Path,
    encoding: str,
    header_sets: list[list[tuple[str, str]]],
    gate: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[list[bytes], SpyRegistry]:
    w = wiring(tmp_path)
    spy = SpyRegistry(w.registry)
    checker = EdgeCert(HEADER, encoding, lambda: (CA_A.cert, CA_B.cert))
    changes = {} if gate is None else {"gate_ok_async": gate}
    app = app_for(
        w, registry=spy, peer_allowed=edge_peer_allowed(ME), client_fingerprint=checker.fingerprint, **changes
    )
    replies = []
    with short_dir() as d:
        async with edge_serving(app, d / "edge.sock", ME):
            for headers in header_sets:
                lines = [*upgrade_headers().items(), ("Authorization", f"Bearer {TOKEN}"), *headers]
                data = b"GET /ws HTTP/1.1\r\nHost: example.com\r\n" + b"".join(
                    f"{k}: {v}\r\n".encode() for k, v in lines
                )
                replies.append(await raw_unix(d / "edge.sock", data + b"\r\n"))
    return replies, spy


@pytest.mark.parametrize("kind", REFUSED_CERTS)
@pytest.mark.parametrize("encoding", ["pem-urlescaped", "der-base64"])
def test_sat_sc_12_certs_are_401_without_a_registry_read(tmp_path, kind, encoding):
    value = encoded(REFUSED_CERTS[kind](), encoding)
    (reply,), spy = asyncio.run(_exchange(tmp_path, encoding, [[(HEADER, value)]]))
    assert reply.startswith(b"HTTP/1.1 401") and reply.endswith(b'{"error":"unauthorized"}')
    assert spy.lookups == []


def test_a_refused_or_missing_certificate_is_401_before_the_allow_all_gate_is_asked(tmp_path):
    """A closed gate alone answers 503: the 401 shows the certificate is judged first, and the gate never runs."""
    asked: list[int] = []

    async def closed_gate() -> bool:
        asked.append(1)
        return False

    sets = [[(HEADER, encoded(make(), "pem-urlescaped"))] for make in REFUSED_CERTS.values()] + [[]]
    replies, spy = asyncio.run(_exchange(tmp_path, "pem-urlescaped", sets, closed_gate))
    assert [r.split(b" ", 2)[1] for r in replies] == [b"401"] * len(sets)
    assert asked == [] and spy.lookups == []
    good = [[(HEADER, encoded(device_cert(CA_A), "pem-urlescaped"))]]
    (tmp_path / "control").mkdir()
    (reply,), _spy = asyncio.run(_exchange(tmp_path / "control", "pem-urlescaped", good, closed_gate))
    assert reply.split(b" ", 2)[1] == b"503" and asked == [1]  # control: a good certificate does reach the closed gate


def test_garbage_repeated_and_missing_certificate_headers_are_401_without_a_registry_read(tmp_path):
    cases = _garbage()
    replies, spy = asyncio.run(_exchange(tmp_path, "pem-urlescaped", list(cases.values())))
    for name, reply in zip(cases, replies, strict=True):
        assert reply.split(b" ", 2)[1] == (b"400" if name == "over-the-cap" else b"401"), name  # 400: aiohttp's 8190 B
    assert spy.lookups == []


@pytest.mark.parametrize("encoding", ["pem-urlescaped", "der-base64"])
def test_a_cert_of_either_ca_reaches_the_token_and_fingerprint_check_with_its_own_fingerprint(tmp_path, encoding):
    first, second = device_cert(CA_A), device_cert(CA_B)
    forged = ("X-Client-Cert-Fp", fingerprint(second))  # a fingerprint header naming another device: ignored
    sets = [[(HEADER, encoded(first, encoding)), forged], [(HEADER, encoded(second, encoding))]]
    replies, spy = asyncio.run(_exchange(tmp_path, encoding, sets))
    assert all(r.startswith(b"HTTP/1.1 401") for r in replies)  # the token is wrong: judged by the registry
    assert spy.lookups == [fingerprint(first), fingerprint(second)]


def test_no_certificate_bytes_token_or_fingerprint_reach_a_response_or_a_log_line(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cert = device_cert(CA_A, start=NOW - timedelta(days=91))  # expired: refused after a full decode
    good = device_cert(CA_B)
    sets = [[(HEADER, encoded(cert, "der-base64"))], [(HEADER, encoded(good, "der-base64"))]]
    replies, _spy = asyncio.run(_exchange(tmp_path, "der-base64", sets))
    secrets = (TOKEN, fingerprint(cert), fingerprint(good), der_base64(cert)[40:100], der_base64(good)[40:100])
    text = b"".join(replies).decode("latin-1") + caplog.text
    text += "".join(str(r.args) + str(r.exc_info) for r in caplog.records)
    assert [s for s in secrets if s in text] == []
