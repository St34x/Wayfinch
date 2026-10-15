# SPDX-License-Identifier: Apache-2.0
"""The ``/enroll`` relay against the fake signer: each signer answer maps to one fixed HTTP answer (SAT-SC-05), the
client's enrol deadline is the only one (SAT-SC-13), a refused enrolment is the same 404 as an unknown path
(SAT-CA-33), success carries exactly {cert, token, deviceId} (SAT-CA-42) and no submitted value, code or token reaches
a body or a log (SAT-CA-39). Input refusals, the peer check, the rate limit and concurrency:
``test_enroll_input``."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import enroll_helpers
import fake_signer
import pytest
from enroll_helpers import BODY, CODE, SIG, SPKI, SUBMITTED, Reply, app_with, exchange, post, relay_for
from fake_signer import Script

from glasses import enroll, signer_client
from glasses.enroll import EnrollRelay, RateLimiter
from glasses.signer_client import SignerClient

fake = enroll_helpers.fake  # the shared fixture, bound here so pytest finds it


def assert_clean(reply: Reply, log: str = "") -> None:
    for value in (*SUBMITTED, fake_signer.TOKEN):
        assert value.encode() not in reply.body
        assert value not in log


# Success (SAT-SC-05, SAT-CA-42 HTTP half)


def test_success_returns_exactly_cert_token_and_device_id_without_caching(fake):
    reply = post(relay_for(fake))
    assert reply.status == 200
    assert reply.headers["Content-Type"].split(";")[0] == "application/json"
    assert reply.headers["Cache-Control"] == "no-store"
    assert json.loads(reply.body) == {
        "cert": fake_signer.CERT_PEM,
        "token": fake_signer.TOKEN,
        "deviceId": fake_signer.DEVICE_ID,
    }


def test_the_signer_gets_the_submitted_fields_with_signature_as_sig(fake):
    post(relay_for(fake))
    assert fake.ops == ["enrol"]
    parsed = fake.requests[0].parsed
    assert parsed is not None
    assert {key: parsed[key] for key in ("spki", "code", "sig")} == {"spki": SPKI, "code": CODE, "sig": SIG}
    assert parsed.keys() == {"v", "op", "rid", "spki", "code", "sig"}


def test_client_cert_and_forwarded_headers_change_no_outcome(fake):
    headers = {"X-Client-Cert": "planted", "X-Forwarded-For": "192.0.2.1", "Forwarded": "for=192.0.2.1"}
    plain, planted = exchange(app_with(relay_for(fake)), {}, {"headers": headers})
    assert (plain.status, plain.body) == (planted.status, planted.body) == (200, plain.body)


# Answer mapping (SAT-SC-05)


@pytest.mark.parametrize(
    ("script", "status"),
    [
        (Script(error="enrol_refused"), 404),
        (Script(error="bad_proof"), 400),
        (Script(error="known_key"), 400),
        (Script(error="bad_request"), 400),
        (Script(error="rate_limited"), 429),
        (Script(error="rate_limited", extras={"retry_after_s": 30}), 429),
        (Script(error="internal"), 503),
        (Script(error="not_initialised"), 503),
        (Script(error="forbidden"), 503),
        (Script(close=True), 503),
        (Script(raw=lambda rid: b"not json\n"), 503),
        (Script(raw=lambda rid: fake_signer.answer("0" * 32, fake_signer.canned("enrol", {}))), 503),
    ],
    ids=[
        "enrol_refused",
        "bad_proof",
        "known_key",
        "bad_request",
        "rate_limited",
        "rate_limited_retry",
        "internal",
        "not_initialised",
        "forbidden",
        "close",
        "raw_non_json",
        "wrong_rid",
    ],
)
def test_each_signer_answer_maps_to_one_fixed_http_answer(fake, caplog, script, status):
    caplog.set_level(logging.DEBUG)
    fake.scripts["enrol"] = script
    reply = post(relay_for(fake))
    assert reply.status == status
    assert "Retry-After" not in reply.headers
    assert fake.ops.count("enrol") == 1
    assert_clean(reply, caplog.text)
    warnings = [r for r in caplog.records if r.name == "glasses.enroll" and r.levelno >= logging.WARNING]
    assert len(warnings) == (1 if status == 503 else 0)


def test_the_generic_400_is_the_same_bytes_for_every_bad_request_cause(fake):
    bodies = set()
    for code in ("bad_proof", "known_key", "bad_request"):
        fake.scripts["enrol"] = Script(error=code)
        bodies.add(post(relay_for(fake)).body)
    bodies.add(post(relay_for(fake), json={**BODY, "code": "short"}).body)
    bodies.add(post(relay_for(fake), json={**BODY, "extra": "x"}).body)
    assert len(bodies) == 1


def test_a_malformed_code_is_400_and_nothing_reaches_the_signer(fake):
    for bad in ({**BODY, "code": "short"}, {**BODY, "spki": "not base64!"}, {**BODY, "signature": "A" * 200}):
        assert post(relay_for(fake), json=bad).status == 400
    assert fake.requests == []


def test_an_absent_signer_socket_is_503(tmp_path, caplog):
    relay = EnrollRelay(lambda: SignerClient(tmp_path / "none.sock", os.geteuid()), limiter=RateLimiter(100))
    assert post(relay).status == 503
    assert "SignerUnavailable" in caplog.text


def test_an_unconfigured_signer_is_503_naming_only_the_kind(caplog):
    def unconfigured() -> SignerClient:
        raise signer_client.NotConfigured("signer.socket")

    reply = post(EnrollRelay(unconfigured, limiter=RateLimiter(100)))
    assert reply.status == 503
    assert [r.getMessage() for r in caplog.records if r.name == "glasses.enroll"] == [
        "enrol relay: signer call failed (NotConfigured)"
    ]
    assert_clean(reply, caplog.text)


# Deadline: SAT-SC-13, scaled


@pytest.mark.parametrize(("delay_s", "status"), [(0.6, 200), (2.0, 503)])
def test_the_client_enrol_deadline_is_the_only_deadline(fake, delay_s, status):
    fake.scripts["enrol"] = Script(delay_s=delay_s)
    assert post(relay_for(fake, enrol_s=1.2)).status == status
    assert fake.ops.count("enrol") == 1


def test_the_relay_adds_no_timeout_of_its_own():
    assert "wait_for" not in Path(enroll.__file__).read_text(encoding="utf-8")


# One 404 for everything unknown (SAT-CA-33, HTTP half)


def test_a_refused_enrolment_a_refused_peer_and_an_unknown_path_look_the_same(fake):
    fake.scripts["enrol"] = Script(error="enrol_refused")
    denied = relay_for(fake, peer_allowed=lambda request: False)
    app = app_with(relay_for(fake), {"/enroll-denied": denied})
    replies = exchange(app, {}, {"path": "/enroll-denied"}, {"path": "/nowhere"})
    seen = {
        (r.status, r.body, tuple(sorted((k, v) for k, v in r.headers.items() if k not in ("Date", "Server"))))
        for r in replies
    }
    assert len(seen) == 1
    assert replies[0].status == 404
    assert fake.ops == ["enrol"]


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_a_refused_peer_gets_the_unknown_path_404_for_any_method(fake, method):
    denied = relay_for(fake, peer_allowed=lambda request: False)
    refused, unknown = exchange(app_with(denied), {"method": method}, {"method": method, "path": "/nowhere"})
    assert (refused.status, refused.body) == (unknown.status, unknown.body) == (404, unknown.body)
    assert "Allow" not in refused.headers
    assert fake.requests == []


# Logs and bodies: SAT-CA-39


def test_no_case_logs_or_echoes_a_submitted_value_or_the_token(fake, caplog):
    caplog.set_level(logging.DEBUG)
    cases: list[dict[str, Any]] = [
        {},
        {"json": {**BODY, "alg": "x"}},
        {"json": [BODY]},
        {"data": b"[" * 4096},
        {"data": json.dumps(BODY).encode() + b" " * 4096},
        {"method": "PUT"},
        {"data": json.dumps(BODY).encode(), "headers": {"Content-Type": "text/plain"}},
    ]
    replies = [post(relay_for(fake), **case) for case in cases]
    for code in ("enrol_refused", "bad_proof", "internal"):
        fake.scripts["enrol"] = Script(error=code)
        replies.append(post(relay_for(fake)))
    assert [r.status for r in replies] == [200, 400, 400, 400, 413, 405, 400, 404, 400, 503]
    for reply in replies[1:]:
        assert_clean(reply, caplog.text)
    assert fake_signer.TOKEN not in caplog.text
