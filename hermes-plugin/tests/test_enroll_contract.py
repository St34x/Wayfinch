# SPDX-License-Identifier: Apache-2.0
"""The README section "HTTP `/enroll`" is the phone's contract for the enrolment request: it is read here and pinned
to the relay's code, so the documented key set and body cap cannot drift from ``glasses.enroll``."""

from __future__ import annotations

import re
from pathlib import Path

from glasses import enroll

README = (Path(__file__).resolve().parents[2] / "protocol" / "README.md").read_text(encoding="utf-8")
HEADING = "\n## HTTP `/enroll`\n"


def section() -> str:
    assert README.count(HEADING) == 1, "protocol/README.md needs exactly one section 'HTTP `/enroll`'"
    return README.split(HEADING, 1)[1].split("\n## ", 1)[0]


def test_the_documented_body_keys_equal_the_relay_keys() -> None:
    table = section().split("| key | value |", 1)[1].split("\n\n", 1)[0]
    documented = re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE)
    assert len(documented) == len(set(documented))
    assert set(documented) == enroll._KEYS


def test_the_documented_body_cap_equals_the_relay_cap() -> None:
    caps = re.findall(r"^\| body cap \| [^|]*?`(\d+)`", section(), re.MULTILINE)
    assert [int(cap) for cap in caps] == [enroll.BODY_CAP]
