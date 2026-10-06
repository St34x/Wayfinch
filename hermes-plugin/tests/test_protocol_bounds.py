# SPDX-License-Identifier: Apache-2.0
"""Contract tests: plugin config bounds vs. the protocol (``protocol/README.md``, read-only).

The byte budgets of ``msg.out`` are parsed from the README limits table, so changing either side alone fails here.
"""

from __future__ import annotations

import json
import re

import pytest

from glasses import config as gcfg

from conftest import PLUGIN_ROOT

PROTOCOL_DIR = PLUGIN_ROOT.parent / "protocol"
ROW_RE = re.compile(r"\|\s*(?P<field>[^|]+?)\s*\|\s*(?P<bytes>[0-9]+)\s*\|")


def envelope_schema() -> dict:
    return json.loads((PROTOCOL_DIR / "envelope.schema.json").read_text(encoding="utf-8"))


def welcome_lanes(schema: dict) -> dict:
    return schema["$defs"]["body.welcome"]["properties"]["lanes"]


def test_max_lanes_equals_welcome_lanes_max_items():
    assert gcfg.MAX_LANES == welcome_lanes(envelope_schema())["maxItems"] == 32


@pytest.mark.parametrize("pattern,schema_def", [(gcfg.LANE_RE, "lane"), (gcfg.PLATFORM_RE, "platform")])
def test_name_patterns_equal_envelope_schema(pattern, schema_def):
    # The config matches in full (re.fullmatch); the schema anchors with ^...$ plus its newline guard.
    assert f"^{pattern.pattern}$" == envelope_schema()["$defs"][schema_def]["pattern"]


def test_32_lanes_accepted_33_refused():
    lanes = [f"lane{i}" for i in range(33)]
    assert gcfg.load({"lanes": lanes[:32], "home_lane": "lane0"}).lanes == tuple(lanes[:32])
    errors = gcfg.check({"lanes": lanes, "home_lane": "lane0"}).errors
    assert [e.split(":", 1)[0] for e in errors] == ["lanes"] and "32" in errors[0]


def readme_limits() -> dict[str, int]:
    """``field -> max UTF-8 bytes`` from the table under "Limits and validation notes"."""
    text = (PROTOCOL_DIR / "README.md").read_text(encoding="utf-8")
    section = text.split("## Limits and validation notes", 1)[1]
    table = section.split("| field | max UTF-8 bytes |", 1)[1].split("\n\n", 1)[0]
    rows = (ROW_RE.fullmatch(line.strip()) for line in table.splitlines())
    return {row["field"]: int(row["bytes"]) for row in rows if row}


def test_readme_table_is_parsed():
    limits = readme_limits()
    assert len(limits) >= 10, limits


@pytest.mark.parametrize(
    "field,constant",
    [
        ("`msg.out.text`", "MSG_OUT_TEXT_MAX_BYTES"),
        ("`msg.out.text` with `longRef`", "MSG_OUT_TEXT_LONGREF_MAX_BYTES"),
        ("`msg.out.longRef.fullText`", "LONG_FULLTEXT_MAX_BYTES"),
    ],
)
def test_budget_constants_equal_readme(field, constant):
    assert getattr(gcfg, constant) == readme_limits()[field]


def test_long_threshold_bound_derives_from_text_budget():
    assert gcfg.MAX_LONG_THRESHOLD == gcfg.MSG_OUT_TEXT_MAX_BYTES // 4 == 512
    assert gcfg.load({"long_threshold": 1}).long_threshold == 1
    assert gcfg.load({"long_threshold": 512}).long_threshold == 512


@pytest.mark.parametrize(
    "text,threshold,expected",
    [
        ("a" * 281, 280, True),
        ("a" * 280, 280, False),
        ("", 1, False),
        ("a", 1, False),
        ("ab", 1, True),
        ("a" * 2049, 100_000, True),  # byte backstop with a threshold passed directly
        ("ж" * 1024, 100_000, False),  # exactly 2048 B
        ("ж" * 1025, 100_000, True),  # 2050 B
        ("\U0001f600" * 512, 512, False),  # 2048 B, 512 characters
        ("\U0001f600" * 513, 100_000, True),  # 2052 B
    ],
    ids=[
        "281-latin",
        "280-latin",
        "empty",
        "one",
        "two",
        "2049B",
        "2048B-cyr",
        "2050B-cyr",
        "2048B-emoji",
        "2052B-emoji",
    ],
)
def test_is_long(text, threshold, expected):
    assert gcfg.is_long(text, threshold) is expected


def test_is_long_counts_lone_surrogates_without_raising():
    assert gcfg.is_long("\ud800" * 683, 100_000) is True  # 3 B each with surrogatepass = 2049 B
    assert gcfg.is_long("\ud800" * 682, 100_000) is False


@pytest.mark.parametrize("name", ["config.py", "adapter.py"])
def test_message_cap_is_not_described_as_a_frame_budget(name):
    # T1.67: 10 752 is where the gateway stops splitting, not what fits one msg.out frame.
    text = (PLUGIN_ROOT / "glasses" / name).read_text(encoding="utf-8")
    assert "fits one protocol frame" not in text and "byte-safe maximum" not in text
    assert "does not split" in text
