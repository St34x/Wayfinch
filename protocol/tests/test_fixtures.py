# SPDX-License-Identifier: Apache-2.0
"""Conformance runner for the golden fixtures in protocol/fixtures (T1.03).

valid/ must pass the envelope schema, invalid/ must fail it. codec-valid/ and codec-invalid/ hold frames for rules
the schema cannot express (UTF-8 byte limits, `seq` direction, nesting depth, lone surrogates, Feb 29); both sets
must be schema-valid here — the codec tests (server and phone) assert accept/reject. index.json lists every file
with its direction and the rule it exercises.
"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from test_schema import DESIGN_TYPES, README_PATH, SCHEMA_PATH
from test_schema_limits import _readme_byte_limits

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
INDEX = json.loads((FIXTURES / "index.json").read_text(encoding="utf-8"))
KINDS = ("valid", "invalid", "codec-valid", "codec-invalid")
NEUTRAL_LANES = {"assistant", "work"}
MAX_FIELD_DEPTH = 32  # README: depth counted from the named open value, which is level 1
PRESCAN_MAX = 35  # README: the raw pre-scan allows 35 nested open brackets for any frame, rejects 36
# Levels a raw-text pre-scan of the whole frame sees above each open value: envelope + body (+ the skills array).
ENCLOSING = {"skill.call.args": 2, "skill.result.output": 2, "skill.manifest.skills[]": 3}


@pytest.fixture(scope="module")
def validator():
    return Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def load(kind, name):
    return json.loads((FIXTURES / kind / name).read_text(encoding="utf-8"))


def entries(kind):
    return [pytest.param(e, id=e["file"]) for e in INDEX[kind]]


def _depth(node):
    if isinstance(node, dict):
        return 1 + max((_depth(v) for v in node.values()), default=0)
    if isinstance(node, list):
        return 1 + max((_depth(v) for v in node), default=0)
    return 0


def _raw_bracket_depth(text):
    """Peak nesting of `{`/`[` outside JSON strings: what a raw-text pre-scan of the whole frame sees."""
    depth = peak = 0
    in_str = esc = False
    for ch in text:
        if in_str:
            esc, in_str = (False, True) if esc else (ch == "\\", ch != '"')
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
            peak = max(peak, depth)
        elif ch in "}]":
            depth -= 1
    return peak


def _field(frame, dotted):
    type_ = frame["type"]
    assert dotted.startswith(type_ + "."), dotted
    node = frame["body"]
    for part in dotted[len(type_) + 1 :].split("."):
        node = node[part]
    return node


def _field_depth(frame, dotted):
    """Depth of the open value named by `dotted`; for `x[]` the deepest item of the list."""
    if dotted.endswith("[]"):
        return max(_depth(item) for item in _field(frame, dotted[:-2]))
    return _depth(_field(frame, dotted))


@pytest.mark.parametrize("kind", KINDS)
def test_index_matches_directory(kind):
    on_disk = sorted(p.name for p in (FIXTURES / kind).glob("*.json"))
    assert on_disk == sorted(e["file"] for e in INDEX[kind])


def test_counts():
    assert len(INDEX["invalid"]) >= 15
    assert {e["type"] for e in INDEX["valid"]} == set(DESIGN_TYPES)


@pytest.mark.parametrize("entry", entries("valid"))
def test_valid_fixture_passes(validator, entry):
    frame = load("valid", entry["file"])
    errs = [e.message for e in validator.iter_errors(frame)]
    assert not errs, errs
    assert frame["type"] == entry["type"]


@pytest.mark.parametrize("entry", entries("invalid"))
def test_invalid_fixture_fails(validator, entry):
    assert entry["reason"]
    assert not validator.is_valid(load("invalid", entry["file"]))


@pytest.mark.parametrize("kind", ["codec-valid", "codec-invalid"])
def test_codec_fixtures_are_schema_valid(validator, kind):
    for e in INDEX[kind]:
        frame = load(kind, e["file"])
        errs = [x.message for x in validator.iter_errors(frame)]
        assert not errs, (e["file"], errs)


@pytest.mark.parametrize("kind", ["codec-valid", "codec-invalid"])
def test_codec_fixture_metadata(kind):
    for e in INDEX[kind]:
        assert e["file"][:4] in ("d2s-", "s2d-"), e["file"]
        assert e["dir"] == e["file"][:3] and e["rule"], e
        frame = load(kind, e["file"])
        if DESIGN_TYPES[frame["type"]] != "both":
            assert {"D→S": "d2s", "S→D": "s2d"}[DESIGN_TYPES[frame["type"]]] == e["dir"], e["file"]
        if "bytes" in e:
            assert len(_field(frame, e["field"]).encode("utf-8")) == e["bytes"], e["file"]
        if "depth" in e:
            assert _field_depth(frame, e["field"]) == e["depth"], e["file"]


def test_codec_fixtures_cover_required_rules():
    files = {e["file"]: e for k in ("codec-valid", "codec-invalid") for e in INDEX[k]}
    names = " ".join(files)
    for needle in (
        "cyrillic",
        "emoji",
        "long-ref",
        "full-text",
        "depth-32",
        "depth-33",
        "skill-result-output-depth",
        "skill-manifest-item-depth",
        "2028-02-29",
        "2027-02-29",
        "lone-surrogate",
        "d2s-error-with-seq",
    ):
        assert needle in names, needle


@pytest.mark.parametrize("kind", ["codec-valid", "codec-invalid"])
def test_byte_fixtures_sit_at_or_over_the_readme_limit(kind):
    readme = _readme_byte_limits()
    sized = [e for e in INDEX[kind] if "bytes" in e]
    assert sized
    for e in sized:
        frame = load(kind, e["file"])
        key = e["field"] + ("+longRef" if e["field"] == "msg.out.text" and "longRef" in frame["body"] else "")
        assert e["limit"] == readme[key], e["file"]
        if kind == "codec-valid":
            assert e["bytes"] == e["limit"], e["file"]
        else:
            assert e["bytes"] > e["limit"], e["file"]


def test_readme_states_the_depth_rule():
    text = " ".join(README_PATH.read_text(encoding="utf-8").split())
    fields = "`skill.call.args`, `skill.result.output` and each `skill.manifest.skills[]` item"
    assert f"Nesting depth of the open objects {fields} (codec, max {MAX_FIELD_DEPTH})" in text
    assert "is level 1, each nested object or array adds 1" in text
    assert f"allows up to {PRESCAN_MAX} nested open brackets" in text
    assert f"for any frame and rejects {PRESCAN_MAX + 1}" in text


def _depth_entries():
    return [(k, e) for k in ("codec-valid", "codec-invalid") for e in INDEX[k] if "depth" in e]


DEPTHS = {
    "s2d-skill-call-args-depth-32.json": (32, 34),
    "s2d-skill-call-args-depth-33.json": (33, 35),
    "d2s-skill-result-output-depth-32.json": (32, 34),
    "d2s-skill-result-output-depth-33.json": (33, 35),
    "d2s-skill-manifest-item-depth-32.json": (32, 35),
    "d2s-skill-manifest-item-depth-33.json": (33, 36),
}  # file -> (per-field depth, raw bracket depth of the whole frame)


def test_open_field_depth_counted_from_the_named_value():
    seen = {}
    for kind, e in _depth_entries():
        depth = _field_depth(load(kind, e["file"]), e["field"])
        assert (depth <= MAX_FIELD_DEPTH) == (kind == "codec-valid"), e["file"]
        seen[e["file"]] = depth
    assert seen == {name: pair[0] for name, pair in DEPTHS.items()}


def test_frame_level_prescan_adds_the_enclosing_levels():
    seen = {}
    for kind, e in _depth_entries():
        raw = _raw_bracket_depth((FIXTURES / kind / e["file"]).read_text(encoding="utf-8"))
        assert raw == e["depth"] + ENCLOSING[e["field"]], e["file"]
        seen[e["file"]] = raw
    assert seen == {name: pair[1] for name, pair in DEPTHS.items()}


def test_every_codec_valid_fixture_passes_the_prescan():
    for e in INDEX["codec-valid"]:
        assert _raw_bracket_depth((FIXTURES / "codec-valid" / e["file"]).read_text(encoding="utf-8")) <= PRESCAN_MAX


@pytest.mark.parametrize(
    ("text", "peak"),
    [('{"a": "[[{{"}', 1), ('{"a": "\\"[{", "b": [{}]}', 3), ('{"a": "\\\\", "b": [1]}', 2), ("[]", 1), ("1", 0)],
)
def test_raw_bracket_depth_ignores_brackets_in_strings(text, peak):
    assert _raw_bracket_depth(text) == peak


def test_lone_surrogate_fixture_is_an_escape():
    raw = (FIXTURES / "codec-invalid" / "d2s-msg-in-lone-surrogate.json").read_text(encoding="utf-8")
    assert "\\ud800" in raw
    assert "\ud800" in load("codec-invalid", "d2s-msg-in-lone-surrogate.json")["body"]["text"]


def test_fixture_lanes_are_neutral():
    for kind in ("valid", "codec-valid", "codec-invalid"):
        for e in INDEX[kind]:
            frame = load(kind, e["file"])
            lanes = [frame.get("lane")] if "lane" in frame else []
            lanes += frame["body"].get("lanes", []) if frame["type"] == "welcome" else []
            chat = frame["body"].get("chatId") if frame["type"] == "msg.out" else None
            if chat:
                lanes.append(chat.split(":", 1)[1])
            assert set(lanes) <= NEUTRAL_LANES, (e["file"], lanes)
