# SPDX-License-Identifier: Apache-2.0
"""config check: values that may carry credentials are never echoed. No Hermes import (conftest.py)."""

from __future__ import annotations

import io
from typing import Any

import pytest

from glasses import admin
from glasses import config as gcfg

SECRET = "REDACTfakeSECRET4410"
PLANTS = (f"https://u:{SECRET}@h.example.com", f"u:{SECRET}@h", f"x://{SECRET}")


def echo_paths(node: dict[str, Any], prefix: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], bool]]:
    """Every schema path marked ``x-echo`` (recursively), with whether the value is a list."""
    out: list[tuple[tuple[str, ...], bool]] = []
    for key, child in node.get("properties", {}).items():
        path = prefix + (key,)
        if child.get("x-echo"):
            out.append((path, child.get("type") == "array"))
        out.extend(echo_paths(child, path))
    return out


def nested(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    data: Any = value
    for key in reversed(path):
        data = {key: data}
    return data


ECHO = echo_paths(gcfg.schema())


def test_echo_fields_are_the_expected_names_and_paths():
    names = {".".join(p) for p, _ in ECHO}
    assert names == {
        "host",
        "state_dir",
        "lanes",
        "home_lane",
        "client_cert_header",
        "client_cert_encoding",
        "long_answers.platform",
        "signer.socket",
        "signer.user",
        "signer.public_dir",
        "meeting.language_hints",
    }


@pytest.mark.parametrize("plant", PLANTS)
@pytest.mark.parametrize("path,is_list", ECHO, ids=[".".join(p) for p, _ in ECHO])
def test_planted_secret_in_every_echo_field_is_not_shown(path, is_list, plant):
    errors = gcfg.check(nested(path, [plant] if is_list else plant)).errors
    assert errors and all(SECRET not in e for e in errors), errors
    assert any("a string of" in e for e in errors), errors


@pytest.mark.parametrize(
    "meeting",
    [
        {"endpoint": f"https://user:{SECRET}@api.example.com"},
        {"endpoint": f"{SECRET}.Example.com"},
        {"provider": f"sk-{SECRET}"},
        {"llm": {"provider": f"Bearer {SECRET}"}},
        {"llm": {"model": f"{SECRET} x"}},
    ],
)
def test_credential_capable_meeting_fields_never_echo(meeting):
    (msg,) = gcfg.check({"meeting": meeting}).errors
    assert SECRET not in msg and "a string of" in msg


def test_plain_names_are_still_echoed():
    (msg,) = gcfg.check({"host": "0.0.0.0"}).errors
    assert '(got "0.0.0.0")' in msg
    assert gcfg.describe("a@b", True) == "a string of 3 characters"
    assert gcfg.describe("s3://x", True) == "a string of 6 characters"


def test_load_exception_carries_no_secret():
    with pytest.raises(gcfg.ConfigError) as exc:
        gcfg.load({"home_lane": f"u:{SECRET}@h", "meeting": {"endpoint": f"https://{SECRET}"}})
    assert SECRET not in str(exc.value)


def test_state_dir_path_with_at_sign_is_not_shown(tmp_path):
    state = tmp_path / f"u:{SECRET}@h"
    state.mkdir(mode=0o755)
    state.chmod(0o755)
    report = admin.Report()
    admin._state_dir_problems(state, report)
    (msg,) = report.errors
    assert SECRET not in msg and "mode 0755" in msg and "not shown" in msg


def test_config_path_with_at_sign_is_not_shown(tmp_path):
    out = io.StringIO()
    path = tmp_path / f"u:{SECRET}@h.yaml"
    assert admin.main(["config", "check", "--config", str(path)], {}, out) == admin.EXIT_UNREADABLE
    assert SECRET not in out.getvalue() and "cannot read (FileNotFoundError)" in out.getvalue()


def test_lane_count_message_matches_max_lanes():
    lanes = gcfg.schema()["properties"]["lanes"]
    assert lanes["x-errors"]["maxItems"] == f"must have at most {gcfg.MAX_LANES} lanes"
    assert str(gcfg.MAX_LANES) in lanes["description"] and lanes["maxItems"] == gcfg.MAX_LANES
