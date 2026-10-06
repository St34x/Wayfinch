# SPDX-License-Identifier: Apache-2.0
"""Maximal frames and timestamp edges for protocol/envelope.schema.json.

Independent of the analytic worst case in test_schema_limits.py: every type gets a real, schema-VALID maximal frame
(every optional field present, every string at maxLength, free text made of distinct astral emoji, integers at
maximum, arrays at maxItems), which is then serialised the way a server could (`json.dumps` with the default
`ensure_ascii=True` and default separators = worst case, and `ensure_ascii=False`) and measured in UTF-8 bytes.

Also: the bidi guard sits on exactly the README-listed display fields and nowhere else; README documents 4409;
out-of-range timestamps are rejected, with calendar/offset edges.
"""

import itertools
import json
import re

import pytest
from jsonschema import Draft202012Validator

import test_schema as base

FRAME_CAP = 65536
AUDIO_CAP = 32768
AUDIO_TYPES = {"audio.chunk", "meeting.audio"}
SCHEMA = json.loads(base.SCHEMA_PATH.read_text(encoding="utf-8"))
README = (base.SCHEMA_PATH.parent / "README.md").read_text(encoding="utf-8")
TYPES = SCHEMA["properties"]["type"]["enum"]
OPEN_OBJECT_TYPES = {"skill.call", "skill.result", "skill.manifest"}

# Maximal ASCII value per known pattern (hand-written, not derived from the schema's own maxLength).
PATTERN_MAX = {
    "^[0-7][0-9A-HJKMNP-TV-Z]{25}$": lambda i: "7" + "Z" * 24 + "0123456789ABCDEFGH"[i % 18],
    "^[A-Za-z0-9._:-]+$": lambda i: "A" * 127 + "0123456789"[i % 10],
    "^[A-Za-z0-9:_-]+$": lambda i: "sha256:" + "f" * 121,
    "^[a-z][a-z0-9_-]{0,31}$": lambda i: "a" * 31 + "abcdefghijklmnopqrstuvwxyz012345"[i % 32],
    "^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$": lambda i: "abc-abcdefgh-abcdefgh-abcdefgh-ab" + "cdefghijkl"[i % 10] + "x",
    "^[a-z][a-z0-9_]{0,63}$": lambda i: "a" * 64,
    "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$": lambda i: "a" * 63 + "." + "a" * 64,
    "^[a-z0-9][a-z0-9_-]{0,63}$": lambda i: "0" * 64,
    "^glasses:[a-z][a-z0-9_-]{0,31}$": lambda i: "glasses:" + "a" * 32,
    "^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$": lambda i: "A" * 31744,
}
TS_MAX = "2026-10-03T18:00:00.123456789+14:00"


def resolve(s):
    while "$ref" in s:
        name = s["$ref"].split("/")[-1]
        s = {**SCHEMA["$defs"][name], **{k: v for k, v in s.items() if k != "$ref"}}
    return s


def emoji_text(n, i):
    # distinct astral code points (12 bytes each when \\u-escaped as a surrogate pair)
    return "".join(chr(0x1F600 + ((i * 7 + k) % 64)) for k in range(n))


def _max_string(s, i):
    if "pattern" not in s:
        return emoji_text(s["maxLength"], i)
    if s["pattern"].startswith("^[0-9]{4}-"):
        return TS_MAX
    return PATTERN_MAX[s["pattern"]](i)


def _max_array(s):
    items = resolve(s["items"])
    if "enum" in items:  # uniqueItems enum arrays: distinct members, longest first
        return sorted(items["enum"], key=lambda e: -len(json.dumps(e)))[: s["maxItems"]]
    return [maxval(s["items"], k) for k in range(s["maxItems"])]


def maxval(s, i=0):
    s = resolve(s)
    if "const" in s:
        return s["const"]
    if "enum" in s:
        return max(s["enum"], key=lambda e: len(json.dumps(e)))
    t = s.get("type")
    if t == "string":
        return _max_string(s, i)
    if t == "integer":
        return s["maximum"]
    if t == "boolean":
        return False
    if t == "array":
        return _max_array(s)
    if t == "object":
        if "properties" not in s:
            return {}  # open object: bounded only by the frame cap (README exception), see separate test
        return {k: maxval(p, i) for k, p in s["properties"].items()}
    raise AssertionError(f"unhandled schema {s}")


def envelope(type_, body):
    f = {
        "v": 1,
        "id": PATTERN_MAX["^[0-7][0-9A-HJKMNP-TV-Z]{25}$"](1),
        "type": type_,
        "ts": TS_MAX,
        "corr": PATTERN_MAX["^[0-7][0-9A-HJKMNP-TV-Z]{25}$"](2),
        "lane": "a" * 32,
        "body": body,
        "seq": 2**53 - 1,  # with_seq_rule() drops it where the type forbids it
    }
    return f


V = Draft202012Validator(SCHEMA)


def with_seq_rule(f):
    # pick whichever of with/without seq is valid (seq is required / forbidden / optional per type)
    a = dict(f, seq=2**53 - 1)
    b = {k: x for k, x in f.items() if k != "seq"}
    return a if V.is_valid(a) else b


def _enum_variants(type_, base_body, enums):
    """Valid frames of `base_body` for every combination of its top-level enum values."""
    keys = [k for k in enums if k in base_body]
    out = []
    for combo in itertools.product(*[enums[k] for k in keys]):
        b = dict(base_body, **dict(zip(keys, combo, strict=True)))
        f = with_seq_rule(envelope(type_, b))
        if V.is_valid(f):
            out.append(f)
    return out


def maximal_frames(type_):
    """All schema-valid maximal candidates for a type (subsets of optional fields x top-level enum values)."""
    bs = resolve(SCHEMA["$defs"]["body." + type_])
    full = maxval(bs)
    req = set(bs.get("required", []))
    opt = [k for k in full if k not in req]
    enums = {k: resolve(p)["enum"] for k, p in bs.get("properties", {}).items() if "enum" in resolve(p)}
    out = []
    for r in range(len(opt) + 1):
        for drop in itertools.combinations(opt, r):
            base_body = {k: x for k, x in full.items() if k not in drop}
            out.extend(_enum_variants(type_, base_body, enums))
    if type_ == "msg.out":  # longRef forces text <= 512: build that variant explicitly
        b = dict(full, kind="answer", text=emoji_text(512, 3))
        b.pop("notice")
        out.append(with_seq_rule(envelope(type_, b)))
    return out


def size(f, ensure_ascii):
    return len(json.dumps(f, ensure_ascii=ensure_ascii).encode("utf-8"))


@pytest.mark.parametrize("type_", TYPES)
def test_real_maximal_frame_is_valid_and_fits_cap(type_):
    frames = maximal_frames(type_)
    assert frames, f"{type_}: could not build any valid maximal frame"
    for f in frames:
        assert V.is_valid(f), [e.message for e in V.iter_errors(f)][:3]
    cap = AUDIO_CAP if type_ in AUDIO_TYPES else FRAME_CAP
    worst = max(size(f, True) for f in frames)
    worst_utf8 = max(size(f, False) for f in frames)
    summary = f"{type_}: worst ensure_ascii=True {worst} B, ensure_ascii=False {worst_utf8} B, cap {cap}"
    assert worst <= cap and worst_utf8 <= cap, summary


def test_msg_out_worst_cases_explicit():
    bs = SCHEMA["$defs"]["body.msg.out"]
    full = maxval(bs)
    full.pop("notice")
    lr = dict(full, kind="answer", text=emoji_text(512, 1))
    nolr = {k: x for k, x in full.items() if k != "longRef"}
    nolr.update(kind="answer", text=emoji_text(2048, 1))
    for b in (lr, nolr):
        f = with_seq_rule(envelope("msg.out", b))
        assert V.is_valid(f)
        assert size(f, True) <= FRAME_CAP
    # text 513 with longRef must be invalid, text 2049 without must be invalid
    assert not V.is_valid(with_seq_rule(envelope("msg.out", dict(lr, text=emoji_text(513, 1)))))
    assert not V.is_valid(with_seq_rule(envelope("msg.out", dict(nolr, text=emoji_text(2049, 1)))))


@pytest.mark.parametrize("type_,path", [("skill.call", "args"), ("skill.result", "output")])
def test_contract_open_object_can_exceed_frame_cap(type_, path):
    # README exception: open objects are bounded by maxProperties only; a schema-valid frame can exceed 64 KiB,
    # so the codec's byte cap (checked before json.loads) is the only bound.
    bs = SCHEMA["$defs"]["body." + type_]
    b = maxval(bs)
    if type_ == "skill.result":
        b["status"] = "ok"
    b[path] = {"k": "x" * 70000}
    f = with_seq_rule(envelope(type_, b))
    assert V.is_valid(f)
    assert size(f, True) > FRAME_CAP


# --- bidi guard: exactly the listed fields ---------------------------------------------------------

BIDI = [
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
]
NEAR_MISS = ["\u200b", "\u200c", "\u200d", "\u2060", "\u202f", "\u2065", "\u206a", "\ufeff", "\u061b"]
GUARDED = {
    ("approval.request", ("summary",)),
    ("approval.request", ("detail",)),
    ("clarify.request", ("question",)),
    ("clarify.request", ("choices", 0)),
}


def free_text_fields():
    """(type, path) for every pattern-free string field in every body (incl. nested objects and array items)."""
    out = []

    def walk(t, s, path):
        s = resolve(s)
        if s.get("type") == "string" and "pattern" not in s and "enum" not in s and "const" not in s:
            out.append((t, path))
        elif s.get("type") == "object" and "properties" in s:
            for k, p in s["properties"].items():
                walk(t, p, path + (k,))
        elif s.get("type") == "array" and "items" in s:
            walk(t, s["items"], path + (0,))

    for t in TYPES:
        walk(t, SCHEMA["$defs"]["body." + t], ())
    return out


def _seed_body(t, path):
    """A valid body of type `t` in the state that lets the field at `path` exist."""
    b = json.loads(json.dumps(base.VALID_BODIES[t]))
    if t == "msg.out" and path[0] == "longRef":
        b["longRef"] = {"id": "x1", "channel": "telegram", "profile": "work", "status": "sent"}
        b["text"] = "short"
    if t == "msg.out" and path[0] == "notice":
        b.update(kind="notice", notice={"kind": "system"})
    if t == "meeting.state" and path == ("error",):
        b["state"] = "error"
    if t == "skill.result" and path == ("error",):
        b["status"] = "error"
    if t == "clarify.response":
        b.pop("dismiss", None)
    return b


def _pad(items, index):
    while len(items) <= index:
        items.append("x")


def _set_path(b, path, value):
    cur = b
    for i, k in enumerate(path[:-1]):
        if isinstance(k, int):
            _pad(cur, k)
            cur = cur[k]
        else:
            cur = cur.setdefault(k, [] if isinstance(path[i + 1], int) else {})
    last = path[-1]
    if isinstance(last, int):
        _pad(cur, last)
    cur[last] = value


def frame_with(t, path, value):
    b = _seed_body(t, path)
    _set_path(b, path, value)
    return with_seq_rule(envelope(t, b) | {"lane": "work"})


def test_free_text_field_inventory_baseline_valid():
    fields = free_text_fields()
    assert len(fields) >= 15
    for t, p in fields:
        f = frame_with(t, p, "plain text")
        assert V.is_valid(f), (t, p, [e.message for e in V.iter_errors(f)][:3])


@pytest.mark.parametrize("ch", BIDI, ids=[f"U+{ord(c):04X}" for c in BIDI])
def test_bidi_rejected_exactly_on_listed_fields(ch):
    rejected = set()
    for t, p in free_text_fields():
        if not V.is_valid(frame_with(t, p, "Run: " + ch + "rm -rf ~")):
            rejected.add((t, p))
    assert rejected == GUARDED, f"rejected={sorted(rejected)}"


@pytest.mark.parametrize("ch", NEAR_MISS, ids=[f"U+{ord(c):04X}" for c in NEAR_MISS])
def test_non_bidi_format_chars_accepted_on_guarded_fields(ch):
    for t, p in GUARDED:
        f = frame_with(t, p, "a" + ch + "b")
        assert V.is_valid(f), (t, p)


def test_readme_documents_4409_and_lists_the_guarded_fields():
    assert re.search(r"`4409`\s*\|\s*`replaced`", README)
    for name in ("approval.request.summary", "clarify.request.question", ".detail", ".choices[]"):
        assert name in README


# --- timestamps ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ts",
    [
        "2026-13-01T00:00:00Z",
        "2026-00-10T00:00:00Z",
        "2026-10-32T00:00:00Z",
        "2026-10-03T24:00:00Z",
        "2026-10-03T18:60:00Z",
        "2026-10-03T18:00:61Z",
        "2026-10-03T18:00:00+25:00",
        "2026-04-31T00:00:00Z",
        "2026-02-30T00:00:00Z",
        "2026-10-00T00:00:00Z",
        "2026-10-03T18:00:00+14:01",
        "2026-10-03T18:00:00-15:00",
        "2026-10-03T18:00:60Z",
        "2026-10-03T18:00:00.Z",
        "2026-10-03T18:00:00.1234567890Z",
        "2026-10-03T18:00:00+1400",
        "2026-10-03T18:00:00",
    ],
)
def test_timestamp_out_of_range_rejected(ts):
    assert not V.is_valid(base.frame("ping", ts=ts))
    assert not V.is_valid(
        with_seq_rule(envelope("approval.request", dict(base.VALID_BODIES["approval.request"], expiresAt=ts)))
    )


@pytest.mark.parametrize(
    "ts",
    [
        "2026-02-29T00:00:00Z",
        "2024-02-29T23:59:59Z",
        "2026-12-31T23:59:59.999999999Z",
        "2026-10-03T18:00:00+14:00",
        "2026-10-03T18:00:00-14:00",
        "2026-01-31T00:00:00Z",
        "2026-04-30T00:00:00+05:30",
        "0000-01-01T00:00:00Z",
    ],
)
def test_timestamp_edges_accepted(ts):
    assert V.is_valid(base.frame("ping", ts=ts))
