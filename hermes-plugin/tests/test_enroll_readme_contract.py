# SPDX-License-Identifier: Apache-2.0
"""``protocol/README.md`` section "HTTP `/enroll`" against the running relay: every number and every status in the
section is checked by sending the request the section describes to ``EnrollRelay`` (fake signer behind it)."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from pathlib import Path

import enroll_helpers
import pytest
from aiohttp.test_utils import TestClient, TestServer
from enroll_helpers import BODY, CODE, JSON, SIG, SPKI, app_with, post, relay_for, send
from fake_signer import Script

from glasses import enroll
from glasses.enroll import BODY_CAP, MAX_IN_FLIGHT, RateLimiter
from glasses.signer_client import Timeouts

fake = enroll_helpers.fake  # the shared fixture, bound here so pytest finds it

README = (Path(__file__).resolve().parents[2] / "protocol" / "README.md").read_text(encoding="utf-8")
MARKER = "planted-marker-must-not-show"


def section() -> str:
    parts = README.split("\n## HTTP `/enroll`\n")
    assert len(parts) == 2
    return parts[1].split("\n## ", 1)[0]


def table_after(header: str) -> list[list[str]]:
    block = section().split(header, 1)[1].split("\n\n", 1)[0]
    rows = [line for line in block.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in row.strip("|").split("|")] for row in rows[1:]]  # [1:] = the |---| line


def documented_statuses() -> set[int]:
    return {int(re.fullmatch(r"`(\d{3})`", row[0]).group(1)) for row in table_after("| status | meaning |")}  # type: ignore[union-attr]


def documented_range(key: str) -> int:
    row = next(r for r in table_after("| key | value |") if r[0] == f"`{key}`")
    return int(re.search(r"\(1\.\.(\d+) bytes\)", row[1]).group(1))  # type: ignore[union-attr]


def b64(size: int, fill: int = 0x41) -> str:
    return base64.b64encode(bytes([fill]) * size).decode()


def body(**override: str) -> bytes:
    return json.dumps({**BODY, **override}).encode()


# --- the numbers in the section are the code's numbers ---


def test_the_section_numbers_are_the_relay_numbers():
    text = section()
    assert documented_range("spki") == 256 and documented_range("signature") == 80
    assert re.search(r"\b22-character base64url code\b", text)
    assert f"`{BODY_CAP}` bytes" in text
    assert re.search(rf"up to {Timeouts().enrol_s:.0f} s", text)  # the answer can take as long as the signer deadline
    assert f"({RateLimiter()._per_minute} a minute in total)" in text
    assert {2: "two"}[MAX_IN_FLIGHT] + " enrolments already running" in text


def test_the_documented_status_set_is_exactly_the_set_the_relay_can_answer(fake):
    seen: set[int] = set()
    for script in (None, Script(error="bad_proof"), Script(error="enrol_refused"), Script(error="internal")):
        fake.scripts["enrol"] = script or Script()
        seen.add(post(relay_for(fake), json=BODY).status)
    seen.add(post(relay_for(fake), data=b"x" * (BODY_CAP + 1), headers=JSON).status)  # 413
    seen.add(post(relay_for(fake), json=BODY, headers={"Content-Type": "text/plain"}).status)  # 400
    limited = relay_for(fake, limiter=RateLimiter(per_minute=1))
    seen.add(enroll_helpers.exchange(app_with(limited), {"json": BODY}, {"json": BODY})[1].status)  # 429
    assert seen == documented_statuses() == {200, 400, 404, 413, 429, 503}


SIGNER_ANSWERS = [
    ("bad_proof", 400),
    ("known_key", 400),
    ("bad_request", 400),
    ("enrol_refused", 404),
    ("rate_limited", 429),
    ("internal", 503),
    ("forbidden", 503),
    ("window_closed", 503),
    ("something_new", 503),
]


@pytest.mark.parametrize(("code", "status"), SIGNER_ANSWERS)
def test_each_signer_refusal_is_the_documented_status(fake, code, status):
    fake.scripts["enrol"] = Script(error=code)
    reply = post(relay_for(fake), json=BODY)
    assert reply.status == status and reply.status in documented_statuses()
    assert reply.headers.get("Cache-Control") != "no-store"


def test_a_closed_connection_and_a_garbled_answer_from_the_signer_are_503(fake):
    for script in (Script(close=True), Script(raw=lambda _rid: f"{MARKER}\n".encode())):
        fake.scripts["enrol"] = script
        reply = post(relay_for(fake), json=BODY)
        assert reply.status == 503 and MARKER.encode() not in reply.body


def test_a_200_carries_exactly_the_three_documented_keys_and_no_store(fake):
    reply = post(relay_for(fake), json=BODY)
    assert reply.status == 200 and reply.headers["Cache-Control"] == "no-store"
    assert set(json.loads(reply.body)) == {"cert", "token", "deviceId"}
    assert reply.headers["Content-Type"].startswith("application/json")


def test_the_404_for_a_refused_enrolment_is_byte_identical_to_an_unknown_path(fake):
    fake.scripts["enrol"] = Script(error="enrol_refused")
    refused, unknown = enroll_helpers.exchange(
        app_with(relay_for(fake)), {"json": BODY}, {"method": "GET", "path": "/no-such-path"}
    )
    assert refused.status == unknown.status == 404
    assert refused.body == unknown.body
    for name in ("Content-Type", "Content-Length"):
        assert refused.headers.get(name) == unknown.headers.get(name)


def test_every_non_200_body_is_a_fixed_text_that_echoes_nothing_sent(fake):
    planted = {"spki": MARKER, "code": MARKER, "signature": MARKER}
    cases = [
        {"data": json.dumps({**planted, MARKER: MARKER}).encode(), "headers": JSON},
        {"data": json.dumps(planted).encode(), "headers": JSON},
        {"data": MARKER.encode(), "headers": JSON},
        {"data": MARKER.encode() * BODY_CAP, "headers": JSON},
        {"data": MARKER.encode(), "headers": {"Content-Type": f"text/{MARKER}"}},
    ]
    for reply in enroll_helpers.exchange(app_with(relay_for(fake)), *cases):
        assert reply.status in (400, 413) and MARKER.encode() not in reply.body
        assert len(reply.body) < 80 and reply.body.decode().startswith(f"{reply.status}: ")
    assert fake.requests == []


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
def test_any_method_but_post_is_405_as_documented(fake, method):
    reply = enroll_helpers.exchange(app_with(relay_for(fake)), {"method": method})[0]
    assert reply.status == 405 and fake.requests == []
    assert "`405`" in section()


# --- the value rules of the key table ---


def test_the_documented_value_sizes_are_accepted_and_one_more_is_refused(fake):
    spki_max, sig_max = documented_range("spki"), documented_range("signature")
    ok = {"spki": b64(spki_max), "signature": b64(sig_max)}
    assert post(relay_for(fake), json={**BODY, **ok}).status == 200
    assert post(relay_for(fake), json={**BODY, "spki": b64(spki_max + 1)}).status == 400
    assert post(relay_for(fake), json={**BODY, "signature": b64(sig_max + 1)}).status == 400
    assert post(relay_for(fake), json={**BODY, "spki": b64(1)}).status == 200  # 1 byte is inside 1..256
    assert [json.loads(r.raw)["spki"] for r in fake.requests][:1] == [ok["spki"]]


@pytest.mark.parametrize(
    "override",
    [
        {"spki": ""},
        {"signature": ""},
        {"code": ""},
        {"code": CODE[:-1]},
        {"code": CODE + "A"},
        {"code": CODE + "=="},  # base64url padding: the code is unpadded
        {"code": CODE.replace(CODE[0], "+", 1)},  # standard alphabet inside a base64url code
        {"spki": b64(25).rstrip("=")},  # unpadded
        {"spki": SPKI[:4] + "\n" + SPKI[4:]},  # line break inside
        {"spki": SPKI + "\n"},
        {"spki": " " + SPKI},
        {"signature": b64(17).rstrip("=")},
        {"signature": base64.urlsafe_b64encode(b"\xfb\xff\xfe\xfa").decode()},  # base64url alphabet ('-', '_')
        {"spki": base64.b64encode(b"x" * 3).decode()[:-1] + "="},  # non-canonical trailing bits
    ],
    ids=[f"value{i}" for i in range(14)],
)
def test_a_value_outside_the_documented_form_is_a_generic_400_and_the_signer_sees_nothing(fake, override):
    reply = post(relay_for(fake), json={**BODY, **override})
    assert reply.status == 400 and fake.requests == []


@pytest.mark.parametrize(
    "payload",
    [
        {**BODY, "name": "phone"},
        {**BODY, "lanes": ["a"]},
        {"spki": SPKI, "code": CODE},
        {"spki": SPKI, "code": CODE, "sig": SIG},  # the signer's name for the field: the phone never sends it
        {**BODY, "sig": SIG},
        {"spki": 1, "code": CODE, "signature": SIG},
        {"spki": None, "code": CODE, "signature": SIG},
        {"spki": [SPKI], "code": CODE, "signature": SIG},
        {"spki": SPKI, "code": CODE, "signature": {"v": SIG}},
        {"spki": SPKI, "code": CODE, "signature": True},
    ],
    ids=["name", "lanes", "no-signature", "sig-instead", "sig-extra", "int", "null", "list", "object", "bool"],
)
def test_the_body_holds_exactly_the_three_string_keys(fake, payload):
    assert post(relay_for(fake), json=payload).status == 400 and fake.requests == []


def test_the_relay_forwards_signature_to_the_signer_as_sig_and_the_phone_never_sends_sig(fake):
    assert post(relay_for(fake), json=BODY).status == 200
    sent = json.loads(fake.requests[0].raw)
    assert sent["sig"] == SIG and "signature" not in sent
    assert {k for k in sent if k not in ("op", "id", "v", "rid")} == {"spki", "code", "sig"}


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Content-Type": "application/json"}, 200),
        ({"Content-Type": "application/json; charset=utf-8"}, 200),
        ({"Content-Type": "text/plain"}, 400),
        ({"Content-Type": "application/jsonx"}, 400),
        ({"Content-Type": "application/x-www-form-urlencoded"}, 400),
        ({"Content-Type": "multipart/form-data; boundary=x"}, 400),
    ],
)
def test_the_content_type_rule(fake, headers, status):
    assert post(relay_for(fake), data=body(), headers=headers).status == status


def test_a_post_with_no_content_type_is_400(fake):
    async def main() -> int:
        app = app_with(relay_for(fake))
        async with (
            TestClient(TestServer(app, host="127.0.0.1")) as client,
            client.post("/enroll", data=body(), skip_auto_headers=["Content-Type"]) as response,
        ):
            return response.status

    assert asyncio.run(main()) == 400 and fake.requests == []


@pytest.mark.parametrize(
    "raw",
    [
        b"\xef\xbb\xbf" + json.dumps(BODY).encode(),  # UTF-8 byte order mark
        json.dumps(BODY).encode("utf-16"),
        json.dumps(BODY).encode() + b" x",
        json.dumps(BODY).encode() + b"{}",
        b"[" * 2000 + b"]" * 2000,
        json.dumps(list(BODY.values())).encode(),
        json.dumps("spki").encode(),
        b"",
        b"null",
        b"\xff\xfe\xfd",
    ],
    ids=["bom", "utf16", "trailing-text", "two-objects", "deep-array", "array", "string", "empty", "null", "bytes"],
)
def test_a_body_that_is_not_one_strict_utf8_object_is_400(fake, raw):
    reply = post(relay_for(fake), data=raw, headers=JSON)
    assert reply.status == 400 and fake.requests == []


def test_whitespace_around_the_object_is_allowed_and_the_cap_counts_bytes(fake):
    base = body()
    exactly = base + b" " * (BODY_CAP - len(base))
    over = exactly + b" "
    assert (len(exactly), len(over)) == (BODY_CAP, BODY_CAP + 1)
    assert post(relay_for(fake), data=exactly, headers=JSON).status == 200
    assert post(relay_for(fake), data=over, headers=JSON).status == 413


def test_a_chunked_body_over_the_cap_is_413_without_a_content_length(fake):
    async def chunks():
        for _ in range(BODY_CAP // 512 + 2):
            yield b" " * 512

    async def main() -> int:
        app = app_with(relay_for(fake))
        async with (
            TestClient(TestServer(app, host="127.0.0.1")) as client,
            client.post("/enroll", data=chunks(), headers=JSON) as response,
        ):
            return response.status

    assert asyncio.run(main()) == 413 and fake.requests == []


def test_multibyte_text_is_counted_in_bytes_not_characters(fake):
    padding = "é" * (BODY_CAP // 2)  # BODY_CAP bytes in BODY_CAP // 2 characters
    raw = json.dumps({**BODY, "x": padding}, ensure_ascii=False).encode()
    assert len(raw) > BODY_CAP
    assert post(relay_for(fake), data=raw, headers=JSON).status == 413


def test_the_documented_key_names_are_the_ones_the_sentinel_body_uses():
    assert set(BODY) == enroll._KEYS and (SPKI, CODE, SIG) == enroll_helpers.SUBMITTED


def test_two_running_enrolments_make_the_third_a_429_and_the_signer_sees_two(fake):
    fake.scripts["enrol"] = Script(delay_s=0.8)

    async def main() -> list[int]:
        relay = relay_for(fake)
        async with TestClient(TestServer(app_with(relay), host="127.0.0.1")) as client:
            held = [asyncio.create_task(send(client)) for _ in range(MAX_IN_FLIGHT)]
            for _ in range(300):
                if len(fake.requests) >= MAX_IN_FLIGHT:
                    break
                await asyncio.sleep(0.01)
            third = await send(client)
            replies = [third, *await asyncio.gather(*held)]
            fourth = await send(client)
        return [r.status for r in replies] + [fourth.status]

    assert asyncio.run(main()) == [429, 200, 200, 200] and len(fake.requests) == MAX_IN_FLIGHT + 1
