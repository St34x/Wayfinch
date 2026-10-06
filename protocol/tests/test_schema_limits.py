# SPDX-License-Identifier: Apache-2.0
"""Size limits of protocol/envelope.schema.json: analytic worst case per type, real
maximal serialisations, README byte-limit table, README and make fragment. Shared helpers: test_schema.py.
"""

import json
import re

import pytest
from jsonschema import Draft202012Validator

from test_schema import (
    DESIGN_TYPES,
    MK_PATH,
    README_PATH,
    ULID,
    ULID2,
    VALID_BODIES,
    assert_invalid,
    assert_valid,
    frame,
)

# --- README and make fragment -------------------------------------------------------------------


def test_readme_lists_every_type():
    text = README_PATH.read_text(encoding="utf-8")
    missing = [t for t in DESIGN_TYPES if f"`{t}`" not in text]
    assert not missing, missing


@pytest.mark.parametrize(
    "code,name", [("4400", "unsupported_version"), ("4401", "unauthorized"), ("4403", "revoked"), ("4409", "replaced")]
)
def test_readme_lists_close_codes(code, name):
    text = README_PATH.read_text(encoding="utf-8")
    assert re.search(rf"^\|\s*`{code}`\s*\|\s*`{name}`", text, re.M), f"close code {code} {name} missing"


def test_make_fragment():
    text = MK_PATH.read_text(encoding="utf-8")
    assert "SPDX-License-Identifier: Apache-2.0" in text.splitlines()[0]
    assert re.search(r"^\.PHONY:.*\btest-protocol\b", text, re.M)
    assert re.search(r"^TEST_TARGETS\s*\+=\s*test-protocol\b", text, re.M)
    assert re.search(r"^test-protocol:", text, re.M)


# --- frame caps and worst-case sizes --------------------------------------------------------------

FRAME_CAP = 65536
AUDIO_FRAME_CAP = 32768
AUDIO_TYPES = {"audio.chunk", "meeting.audio"}
OPEN_FIELD_TYPES = {"skill.call", "skill.result", "skill.manifest"}  # open objects: bounded by the codec frame cap
WORST_BYTES_PER_FREE_CODE_POINT = 12  # astral code point escaped as a \uXXXX\uXXXX surrogate pair


def _resolve(schema, node):
    while "$ref" in node:
        ref = node["$ref"]
        target = schema["$defs"][ref.split("/")[-1]]
        node = {**target, **{k: v for k, v in node.items() if k not in ("$ref", "description")}}
    return node


def _merge(base, override):
    out = dict(base)
    for k, v in override.items():
        if k in ("maxLength", "maxItems", "maximum", "maxProperties") and k in out:
            out[k] = min(out[k], v)
        else:
            out[k] = v
    return out


def _members_size(schema, props):
    """Serialised size of `{"k": v, ...}` with every value at its worst case."""
    return 2 + sum(len(json.dumps(k)) + 1 + _worst(schema, v) + 1 for k, v in props.items())


def _narrowed(schema, props, conds, mask):
    """Properties when the conditionals selected by the bits of `mask` apply (their key present) and the rest not."""
    p = dict(props)
    for i, c in enumerate(conds):
        (key,) = c["if"]["required"]
        if mask >> i & 1:
            for k, o in c["then"].get("properties", {}).items():
                p[k] = _merge(_resolve(schema, p[k]), o)
        else:
            p.pop(key, None)
    return p


def _worst_object(schema, node):
    if node.get("additionalProperties") is not False:
        return 2
    props = node["properties"]
    # conditionals of the form {"if": {"required": [k]}, "then": {"properties": {...}}} narrow other fields
    conds = [c for c in node.get("allOf", []) if c.get("if", {}).keys() == {"required"}]
    return max(_members_size(schema, _narrowed(schema, props, conds, mask)) for mask in range(2 ** len(conds)))


def _worst_string(node):
    n = node["maxLength"]
    if "pattern" in node:
        assert node["pattern"].isascii()
        return n + 2  # restricted printable ASCII, never escaped
    return n * WORST_BYTES_PER_FREE_CODE_POINT + 2


def _worst(schema, node):
    """Upper bound of the serialised size in bytes of any instance valid against `node` (open objects count as {})."""
    node = _resolve(schema, node)
    if "const" in node:
        return len(json.dumps(node["const"]))
    if "enum" in node:
        return max(len(json.dumps(x)) for x in node["enum"])
    t = node.get("type")
    if t == "string":
        return _worst_string(node)
    if t == "integer":
        return max(len(str(node["maximum"])), len(str(node.get("minimum", 0))))
    if t == "boolean":
        return 5
    if t == "array":
        return 2 + node["maxItems"] * (_worst(schema, node["items"]) + 1)
    if t == "object":
        return _worst_object(schema, node)
    raise AssertionError(f"unbounded node {node}")


@pytest.mark.parametrize("type_", sorted(DESIGN_TYPES))
def test_worst_case_frame_fits_cap(schema, type_):
    env = {k: v for k, v in schema["properties"].items() if k not in ("type", "body")}
    size = _members_size(schema, env)
    size += len('"type":') + len(json.dumps(type_)) + 1
    size += len('"body":') + _worst(schema, schema["$defs"]["body." + type_])
    cap = AUDIO_FRAME_CAP if type_ in AUDIO_TYPES else FRAME_CAP
    assert size <= cap, f"{type_}: worst case {size} B > cap {cap} B"


def _max_msg_out(ch, text_len, full_len):
    return frame(
        "msg.out",
        body={
            "kind": "answer",
            "text": ch * text_len,
            "chatId": "glasses:" + "a" * 32,
            "final": True,
            "longRef": {
                "id": "a" * 128,
                "channel": "a" * 32,
                "profile": "0" * 64,
                "status": "pending",
                "fullText": ch * full_len,
            },
        },
        seq=2**53 - 1,
        corr=ULID2,
        lane="a" * 32,
        ts="2026-10-03T18:00:00.123456789+14:00",
    )


@pytest.mark.parametrize("ch", ["x", "ж", "\U0001f600", "\x01"], ids=["ascii", "cyrillic", "emoji", "control"])
@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_max_msg_out_real_serialisation_fits(validator, ch, ensure_ascii):
    f = _max_msg_out(ch, 512, 4096)
    assert_valid(validator, f)
    assert len(json.dumps(f, ensure_ascii=ensure_ascii).encode("utf-8")) <= FRAME_CAP


def test_long_ref_truncated_flag(validator, schema):
    lr = {"id": "long-1", "channel": "telegram", "profile": "work", "status": "sent"}
    body = dict(VALID_BODIES["msg.out"], longRef=dict(lr, fullText="start of", truncated=True))
    assert_valid(validator, frame("msg.out", body=body))
    body["longRef"]["truncated"] = False
    assert_valid(validator, frame("msg.out", body=body))
    body["longRef"]["truncated"] = "yes"
    assert_invalid(validator, frame("msg.out", body=body))
    assert_invalid(validator, frame("msg.out", body=dict(body, longRef=dict(lr, truncated=True))))
    Draft202012Validator.check_schema(schema)


def test_msg_out_text_short_with_long_ref(validator):
    assert_invalid(validator, _max_msg_out("x", 513, 1))
    assert_invalid(validator, _max_msg_out("x", 1, 4097))
    assert_valid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], text="x" * 2048)))
    assert_invalid(validator, frame("msg.out", body=dict(VALID_BODIES["msg.out"], text="x" * 2049)))


@pytest.mark.parametrize("type_", sorted(AUDIO_TYPES))
@pytest.mark.parametrize("ensure_ascii", [True, False])
def test_max_audio_frame_real_serialisation_fits(validator, type_, ensure_ascii):
    chunk = "A" * 31744
    body = (
        {"clipId": "a" * 128, "chunk": chunk}
        if type_ == "audio.chunk"
        else {"meetingId": ULID, "seq": 2**53 - 1, "mime": "audio/ogg;codecs=opus", "chunk": chunk}
    )
    f = frame(type_, body=body, corr=ULID2, lane="a" * 32, ts="2026-10-03T18:00:00.123456789+14:00")
    assert_valid(validator, f)
    assert len(json.dumps(f, ensure_ascii=ensure_ascii).encode("utf-8")) <= AUDIO_FRAME_CAP
    body["chunk"] = chunk + "AAAA"
    assert_invalid(validator, frame(type_, body=body))


def _readme_byte_limits():
    text = README_PATH.read_text(encoding="utf-8")
    section = text[text.index("| field | max UTF-8 bytes |") :]
    rows = {}
    for line in section.splitlines()[2:]:
        if not line.startswith("|"):
            break
        fields, limit = [c.strip() for c in line.strip("|").split("|")]
        if "with `longRef`" in fields:
            rows["msg.out.text+longRef"] = int(limit)
            continue
        for name in re.findall(r"`([^`]+)`", fields):
            rows[name] = int(limit)
    return rows


def _schema_max_length(schema, dotted):
    type_, path = None, None
    for t in sorted(DESIGN_TYPES, key=len, reverse=True):
        if dotted.startswith(t + "."):
            type_, path = t, dotted[len(t) + 1 :]
            break
    node = schema["$defs"]["body." + type_]
    for part in path.split("."):
        node = _resolve(schema, node)
        if part.endswith("[]"):
            node = _resolve(schema, node["properties"][part[:-2]])["items"]
        else:
            node = node["properties"][part]
    return _resolve(schema, node)["maxLength"]


def test_readme_byte_limits_match_schema_max_length(schema):
    rows = _readme_byte_limits()
    assert len(rows) >= 18
    for name, limit in rows.items():
        if name == "msg.out.text+longRef":
            then = schema["$defs"]["body.msg.out"]["allOf"][1]["then"]["properties"]["text"]
            assert then["maxLength"] == limit
            continue
        assert _schema_max_length(schema, name) == limit, name


def test_readme_states_byte_rule_and_codec():
    text = README_PATH.read_text(encoding="utf-8")
    assert "String limits are UTF-8 byte limits" in text
    assert "T1.06" in text
    assert "UTF-16" in text
