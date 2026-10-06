# SPDX-License-Identifier: Apache-2.0
"""The two independently written size helpers (analytic `_worst`, real `maximal_frames`/`maxval`) and the
`frame_with` path setter are cross-checked against each other, so a refactor slip in either side fails here.
"""

import json

import pytest

import test_schema_maximal_frames as maximal
from test_schema_limits import _members_size, _worst


def _analytic_frame_bound(type_):
    env = {k: v for k, v in maximal.SCHEMA["properties"].items() if k not in ("type", "body")}
    size = _members_size(maximal.SCHEMA, env)
    size += len('"type":') + len(json.dumps(type_)) + 1
    return size + len('"body":') + _worst(maximal.SCHEMA, maximal.SCHEMA["$defs"]["body." + type_])


@pytest.mark.parametrize("type_", sorted(set(maximal.TYPES) - maximal.OPEN_OBJECT_TYPES))
def test_analytic_bound_covers_every_real_maximal_frame(type_):
    bound = _analytic_frame_bound(type_)
    # the analytic bound assumes compact separators; the realised frames are re-serialised the same way
    worst = max(len(json.dumps(f, separators=(",", ":")).encode()) for f in maximal.maximal_frames(type_))
    assert worst <= bound, f"{type_}: real maximal frame {worst} B > analytic bound {bound} B"


@pytest.mark.parametrize("type_", sorted(maximal.TYPES))
def test_maximal_frames_only_use_maxval_values(type_):
    # every candidate body is maxval's body minus optional keys, with top-level enum values swapped in
    full = maximal.maxval(maximal.SCHEMA["$defs"]["body." + type_])
    for f in maximal.maximal_frames(type_):
        assert set(f["body"]) <= set(full), type_
        for k, v in f["body"].items():
            node = maximal.resolve(maximal.SCHEMA["$defs"]["body." + type_]["properties"][k])
            assert v == full[k] or v in node.get("enum", []) or (type_, k) == ("msg.out", "text"), (type_, k)


@pytest.mark.parametrize(
    "path,expected",
    [
        (("k",), {"k": "v"}),
        (("k", 0), {"k": ["v"]}),
        (("k", 2), {"k": ["x", "x", "v"]}),
        (("k", "n", 0), {"k": {"n": ["v"]}}),
    ],
)
def test_set_path_builds_missing_containers(path, expected):
    b = {}
    maximal._set_path(b, path, "v")
    assert b == expected


def test_frame_with_keeps_seed_and_sets_only_the_path():
    for t, p in maximal.free_text_fields():
        seed = maximal._seed_body(t, p)
        f = maximal.frame_with(t, p, "PLANTED-VALUE")
        cur = f["body"]
        for k in p:
            cur = cur[k]
        assert cur == "PLANTED-VALUE", (t, p)
        assert f["lane"] == "work"
        for k, v in seed.items():
            if k != p[0]:
                assert f["body"][k] == v, (t, p, k)
