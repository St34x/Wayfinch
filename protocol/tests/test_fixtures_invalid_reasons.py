# SPDX-License-Identifier: Apache-2.0
"""Protocol fixtures: every invalid fixture fails ONLY for its stated reason.

Each invalid fixture's schema errors must all sit at the expected JSON path, and a minimally corrected copy must
validate (so the fixture is not invalid for a second, hidden reason). Byte counts in codec file names are recomputed
independently of index.json.
"""

import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

PROTO = Path(__file__).resolve().parent.parent
FIX = PROTO / "fixtures"
INDEX = json.loads((FIX / "index.json").read_text(encoding="utf-8"))
V = Draft202012Validator(json.loads((PROTO / "envelope.schema.json").read_text(encoding="utf-8")))


def _drop_extra(node_getter):
    def fix(f):
        node = node_getter(f)
        for err in V.iter_errors(f):
            if err.validator == "additionalProperties":
                for key in re.findall(r"'([^']+)'", err.message):
                    node.pop(key, None)

    return fix


def _set(path, value):
    def fix(f):
        node = f
        for part in path[:-1]:
            node = node[part]
        node[path[-1]] = value

    return fix


def _del(key):
    return lambda f: f.pop(key)


# file -> (error path every schema error must be at, minimal correction or None when no single-field fix exists)
CASES = {
    "approval-summary-bidi.json": ("body/summary", _set(["body", "summary"], "ok")),
    "bad-base64-alphabet.json": ("body/chunk", _set(["body", "chunk"], "AAAA")),
    "bad-base64-length.json": ("body/chunk", _set(["body", "chunk"], "AAAA")),
    "bad-base64-padding.json": ("body/chunk", _set(["body", "chunk"], "AAAA")),
    "bad-ts-month.json": ("ts", _set(["ts"], "2026-10-03T18:00:00Z")),
    "bad-ts-no-offset.json": ("ts", _set(["ts"], "2026-10-03T18:00:00Z")),
    "bad-ulid.json": ("id", _set(["id"], "01JABCDEFGHJKMNPQRSTVWXYZ0")),
    "clarify-card-expired.json": ("body", None),
    "clarify-request-expires-at.json": ("body", _drop_extra(lambda f: f["body"])),
    "extra-body-field.json": ("body", _drop_extra(lambda f: f["body"])),
    "extra-envelope-field.json": ("", _drop_extra(lambda f: f)),
    "lane-too-long.json": ("lane", _set(["lane"], "work")),
    "lane-trailing-cr.json": ("lane", _set(["lane"], "work")),
    "lane-trailing-lf.json": ("lane", _set(["lane"], "work")),
    "lane-uppercase.json": ("lane", _set(["lane"], "work")),
    "long-ref-truncated-string.json": ("body/longRef/truncated", _set(["body", "longRef", "truncated"], True)),
    "long-ref-truncated-without-full-text.json": ("body/longRef", _set(["body", "longRef", "fullText"], "x")),
    "missing-body.json": ("", None),
    "missing-seq-on-msg-out.json": ("", _set(["seq"], 1)),
    "oversize-long-ref-text.json": ("body/text", _set(["body", "text"], "x")),
    "oversize-msg-in-text.json": ("body/text", _set(["body", "text"], "x")),
    "oversize-msg-out-text.json": ("body/text", _set(["body", "text"], "x")),
    "seq-on-msg-in.json": ("", _del("seq")),
    "seq-on-ping.json": ("", _del("seq")),
    "unknown-type.json": ("type", None),
    "wrong-version-2.json": ("v", _set(["v"], 1)),
    "wrong-version-string.json": ("v", _set(["v"], 1)),
}


def _load(kind, name):
    return json.loads((FIX / kind / name).read_text(encoding="utf-8"))


def test_every_invalid_fixture_has_a_case_here():
    assert sorted(e["file"] for e in INDEX["invalid"]) == sorted(CASES)


@pytest.mark.parametrize("name", sorted(CASES))
def test_invalid_fixture_errors_only_at_stated_path(name):
    errs = list(V.iter_errors(_load("invalid", name)))
    assert errs
    paths = {"/".join(map(str, e.absolute_path)) for e in errs}
    assert paths == {CASES[name][0]}, (name, paths)


@pytest.mark.parametrize("name", sorted(n for n, (_, fix) in CASES.items() if fix))
def test_corrected_copy_validates(name):
    frame = copy.deepcopy(_load("invalid", name))
    CASES[name][1](frame)
    errs = [e.message for e in V.iter_errors(frame)]
    assert not errs, (name, errs)


def test_truncated_true_without_full_text_rejected_and_false_alone_accepted():
    frame = _load("invalid", "long-ref-truncated-without-full-text.json")
    assert frame["body"]["longRef"].get("truncated") is True
    assert "fullText" not in frame["body"]["longRef"]
    frame["body"]["longRef"]["truncated"] = False
    # dependentRequired fires on presence of the key, not its value: false without fullText is still rejected
    assert not V.is_valid(frame)
    del frame["body"]["longRef"]["truncated"]
    assert V.is_valid(frame)


@pytest.mark.parametrize("kind", ["codec-valid", "codec-invalid"])
def test_byte_counts_in_file_names_recomputed(kind):
    for e in INDEX[kind]:
        m = re.search(r"-(\d+)b-", e["file"])
        if not m:
            continue
        frame = _load(kind, e["file"])
        node = frame["body"]
        for part in e["field"].split(".")[len(frame["type"].split(".")) :]:
            node = node[part]
        assert len(node.encode("utf-8")) == int(m.group(1)) == e["bytes"], e["file"]


def test_index_files_unique_and_no_stray_files():
    names = [e["file"] for k in ("valid", "invalid", "codec-valid", "codec-invalid") for e in INDEX[k]]
    assert len(names) == len(set(names))
    for sub in FIX.iterdir():
        if sub.is_dir():
            assert sub.name in INDEX, sub.name
            assert all(p.suffix == ".json" for p in sub.iterdir()), sub.name
