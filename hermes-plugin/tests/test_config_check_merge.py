# SPDX-License-Identifier: Apache-2.0
"""``config check`` evaluates the glasses platform as the gateway merges it -- platform-level
keys promoted into ``extra``, every block location (``glasses.hermes_merge``). Unit tests (no Hermes import,
conftest.py) plus a differential run against the pinned Hermes merge code in a subprocess."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from glasses import admin
from glasses.hermes_merge import platform_view

from test_config_check_live import WORKTREE, hermes_python

EXTRA = {"public_url": "wss://glasses.example.com/ws", "lanes": ["assistant"]}


def run(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    out = io.StringIO()
    return admin.main(["config", "check", "--config", str(path)], {}, out), out.getvalue()


@pytest.mark.parametrize(
    "data,needle",
    [
        (
            {"gateway": {"platforms": {"glasses": {"enabled": True, "host": "0.0.0.0", "extra": EXTRA}}}},
            "error: glasses.host: must be a loopback IP literal",
        ),
        (
            {"gateway": {"platforms": {"glasses": {"enabled": True, "allow_from": ["*"], "extra": EXTRA}}}},
            "error: glasses.allow_from: not allowed",
        ),
        (
            {"gateway": {"platforms": {"glasses": {"enabled": True, "dm_policy": "open", "extra": EXTRA}}}},
            "error: glasses.dm_policy: not allowed",
        ),
        ({"platforms": {"glasses": {"enabled": True, "extra": {**EXTRA, "port": "x"}}}}, "error: glasses.port:"),
        (
            {"gateway": {"glasses": {"enabled": True, "extra": EXTRA, "allow_admin_from": ["a"]}}},
            "error: glasses.allow_admin_from: not allowed",
        ),
        ({"glasses": {"enabled": True, "host": "0.0.0.0", "extra": EXTRA}}, "error: glasses.host:"),
        (
            {"gateway": {"platforms": {"glasses": {"enabled": True, "extra": EXTRA}}}, "glasses": {"allow_from": "*"}},
            "error: glasses.allow_from: not allowed",
        ),
    ],
)
def test_keys_outside_extra_are_validated_like_the_gateway(tmp_path, data, needle):
    code, out = run(tmp_path, data)
    assert code == admin.EXIT_ERRORS and needle in out, out


def test_explicit_extra_wins_over_a_platform_level_key(tmp_path):
    data = {
        "gateway": {"platforms": {"glasses": {"enabled": True, "host": "0.0.0.0", "extra": {**EXTRA, "host": "::1"}}}}
    }
    code, out = run(tmp_path, data)
    assert code == admin.EXIT_OK, out


def test_typed_keys_stay_out_of_extra_and_root_enabled_wins():
    view = platform_view({
        "gateway": {"platforms": {"glasses": {"enabled": False, "token": "t", "extra": EXTRA}}},
        "glasses": {"enabled": True},
    })
    assert view is not None and view.enabled is True and view.extra == EXTRA


def test_later_block_wins_and_extra_merges():
    view = platform_view({
        "gateway": {"platforms": {"glasses": {"port": 1, "extra": {"lanes": ["a"]}}}},
        "platforms": {"glasses": {"port": 2, "extra": {"home_lane": "a"}}},
    })
    assert view is not None and view.extra == {"port": 2, "lanes": ["a"], "home_lane": "a"}


def test_root_block_authored_extra_wins_over_root_keys():
    view = platform_view({
        "gateway": {"platforms": {"glasses": {"extra": {"host": "::1"}}}},
        "glasses": {"host": "0.0.0.0", "port": 9, "extra": {"bridge_port": 10}},
    })
    assert view is not None and view.extra == {"host": "::1", "port": 9, "bridge_port": 10}


@pytest.mark.parametrize("data", [None, {}, {"gateway": {"platforms": {"glasses": [1]}}}, {"glasses": "x"}])
def test_no_block_is_none(data):
    assert platform_view(data) is None


def test_non_mapping_extra_is_reported_not_dropped(tmp_path):
    code, out = run(tmp_path, {"gateway": {"platforms": {"glasses": {"enabled": True, "extra": [1]}}}})
    assert code == admin.EXIT_ERRORS and "error: glasses.extra: must be a mapping" in out, out


# -- differential: the pinned Hermes merge (merge_platform_sections + PlatformConfig.from_dict) ---------------

CASES = [
    {"gateway": {"platforms": {"glasses": {"enabled": True, "host": "0.0.0.0", "allow_from": ["*"], "extra": EXTRA}}}},
    {"gateway": {"platforms": {"glasses": {"host": "0.0.0.0", "token": "t", "extra": {"host": "::1"}}}}},
    {
        "gateway": {
            "platforms": {"glasses": {"enabled": True, "port": 1, "dm_policy": "open", "extra": {"lanes": []}}}
        },
        "platforms": {"glasses": {"enabled": False, "port": 2, "extra": {"home_lane": ""}}},
    },
    {"platforms": {"glasses": {"typing_indicator": False, "reply_to_mode": "off", "group_policy": "open"}}},
]
RUNNER = """\
import json, sys
from gateway.config import PlatformConfig
from gateway.config_loader import merge_platform_sections
out = []
for case in json.load(open(sys.argv[1])):
    block = merge_platform_sections(case, case.get("gateway"), {})["glasses"]
    cfg = PlatformConfig.from_dict(block)
    out.append({"enabled": cfg.enabled, "extra": {k: v for k, v in cfg.extra.items() if k != "_enabled_explicit"}})
print(json.dumps(out))
"""


def test_view_equals_the_pinned_hermes_merge():
    python = hermes_python()
    base = WORKTREE / ".local" / "hh-t154"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        (Path(tmp) / "cases.json").write_text(json.dumps(CASES), encoding="utf-8")
        (Path(tmp) / "runner.py").write_text(RUNNER, encoding="utf-8")
        (Path(tmp) / "home").mkdir()
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "HERMES_HOME": str(Path(tmp) / "home")}
        proc = subprocess.run(
            [str(python), "runner.py", "cases.json"], cwd=tmp, env=env, capture_output=True, text=True, timeout=240
        )
    assert proc.returncode == 0, proc.stderr[-2000:]
    hermes = json.loads(proc.stdout.strip().splitlines()[-1])
    for case, expected in zip(CASES, hermes, strict=True):
        view = platform_view(case)
        assert view is not None and view.extra == expected["extra"], (case, expected)
        assert (view.enabled is True) == expected["enabled"], (case, expected)
