# SPDX-License-Identifier: Apache-2.0
"""Property tests of the signer protocol (Hypothesis): on any bytes or any JSON shape the decoder returns a result or
raises one of its typed errors - never another exception; an extra key in any answer is refused; any valid enrol
proof round-trips through the request line; any other field text either builds a request under the cap or is a typed
refusal."""

from __future__ import annotations

import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from signer_vectors import FIELDS, RID, VALID, answer, b64, b64url, request

from glasses import signer_protocol as sp
from glasses.signer_protocol import AnswerSchemaError, RequestRefused, SignerError, build_request, decode_answer

# Reproducible in CI: fixed seed, nothing written to a local example database.
PROPERTY = settings(
    max_examples=300, deadline=None, database=None, derandomize=True, suppress_health_check=[HealthCheck.too_slow]
)
OPS = st.sampled_from(sorted(sp.OPS))
JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False, allow_infinity=False) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=12,
)


def decode_or_typed_error(op: str, raw: bytes) -> dict[str, object] | None:
    try:
        return decode_answer(request(op), raw)
    except SignerError as caught:
        assert caught.__context__ is None and str(caught) == caught.message
        return None


@PROPERTY
@given(op=OPS, raw=st.binary(max_size=512))
def test_any_bytes_decode_to_a_result_or_a_typed_error(op: str, raw: bytes) -> None:
    decode_or_typed_error(op, raw)
    decode_or_typed_error(op, raw + b"\n")


@PROPERTY
@given(op=OPS, result=st.dictionaries(st.text(max_size=12), JSON_VALUES, max_size=6), ok=st.booleans())
def test_any_json_object_after_a_valid_envelope_decodes_only_when_it_is_the_exact_result(
    op: str, result: dict[str, Any], ok: bool
) -> None:
    message = {**result, "v": 1, "rid": RID, "ok": ok}
    decoded = decode_or_typed_error(op, json.dumps(message).encode() + b"\n")
    assert decoded is None or (ok and decoded == {k: v for k, v in message.items() if k not in ("v", "rid", "ok")})


@PROPERTY
@given(op=OPS, key=st.text(min_size=1, max_size=12), value=JSON_VALUES)
def test_an_extra_key_in_a_valid_answer_is_always_refused(op: str, key: str, value: Any) -> None:
    if key in VALID[op] or key in ("v", "rid", "ok"):
        return
    try:
        decode_answer(request(op), answer({**VALID[op], key: value}))
    except AnswerSchemaError:
        return
    raise AssertionError("an answer with an extra key was accepted")


@PROPERTY
@given(
    spki=st.binary(min_size=1, max_size=256),
    code=st.binary(min_size=16, max_size=16),
    sig=st.binary(min_size=1, max_size=80),
)
def test_any_valid_enrol_proof_round_trips_through_the_request_line(spki: bytes, code: bytes, sig: bytes) -> None:
    fields = {"spki": b64(spki), "code": b64url(code), "sig": b64(sig)}
    built = build_request("enrol", RID, fields)
    assert len(built.line) <= sp.REQUEST_CAP and built.line.endswith(b"\n") and built.line.count(b"\n") == 1
    assert json.loads(built.line) == {"v": 1, "op": "enrol", "rid": RID, **fields}


@PROPERTY
@given(op=st.sampled_from(sorted(FIELDS)), text=st.text(max_size=400), rid=st.text(max_size=40) | st.just(RID))
def test_any_field_text_builds_a_request_under_the_cap_or_is_a_typed_refusal(op: str, text: str, rid: str) -> None:
    fields = dict.fromkeys(FIELDS[op], text)
    try:
        built = build_request(op, rid, fields)
    except RequestRefused:
        return
    assert len(built.line) <= sp.REQUEST_CAP and json.loads(built.line)["rid"] == rid
