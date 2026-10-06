# SPDX-License-Identifier: Apache-2.0
"""``config check``: the real CLI as a subprocess on planted mistakes with a
recognisable fake secret, the README contract test proven against a mutated copy, three-way lane bounds,
gateway-level keys and ``--hermes-home``. No Hermes import (conftest.py)."""

from __future__ import annotations

import copy
import io
import json
import logging
import os
import subprocess
import sys

import pytest
import yaml

from glasses import admin
from glasses import config as gcfg

import test_protocol_bounds as bounds
from conftest import PLUGIN_ROOT

EXAMPLE = PLUGIN_ROOT.parent / "deploy" / "examples" / "config.example.yaml"
SECRET = "CLIfakeSECRET7731"
BASE = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def write(tmp_path, mutate=None, name="config.yaml"):
    data = copy.deepcopy(BASE)
    if mutate:
        mutate(data["gateway"]["platforms"]["glasses"]["extra"], data)
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def cli(path, tmp_path, env_extra=None):
    """``python -m glasses.admin config check --config PATH`` exactly as a deployer runs it."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HERMES_HOME": str(tmp_path), **(env_extra or {})}
    proc = subprocess.run(
        [sys.executable, "-m", "glasses.admin", "config", "check", "--config", str(path)],
        cwd=PLUGIN_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout, proc.stderr


def setter(key, value):
    def mutate(extra, _data):
        extra[key] = value

    return mutate


PLANTED = [
    ("host", "0.0.0.0", "glasses.host:"),
    ("host", "localhost", "glasses.host:"),
    ("host", "::ffff:127.0.0.1", "glasses.host:"),
    ("port", "8765", "glasses.port:"),
    ("bridge_port", 8765, "glasses.bridge_port:"),
    ("public_url", f"wss://admin:{SECRET}@glasses.example.com/ws", "glasses.public_url:"),
    ("public_url", f"wss://glasses.example.com/ws?token={SECRET}", "glasses.public_url:"),
    ("public_url", f"https://glasses.example.com/{SECRET}", "glasses.public_url:"),
    ("lanes", ["assistant", "work\r"], "glasses.lanes[1]:"),
    ("lanes", [f"l{i}" for i in range(33)], "glasses.lanes:"),
    ("lanes", ["assistant", "work", "work"], "glasses.lanes:"),
    ("home_lane", "elsewhere", "glasses.home_lane:"),
    ("long_threshold", 0, "glasses.long_threshold:"),
    ("long_threshold", 513, "glasses.long_threshold:"),
    ("long_threshold", True, "glasses.long_threshold:"),
    ("long_threshold", "280", "glasses.long_threshold:"),
    ("long_threshold", 280.0, "glasses.long_threshold:"),
    ("long_answers", {"platform": "glasses"}, "glasses.long_answers.platform:"),
    ("client_cert_encoding", "pem", "glasses.client_cert_encoding:"),
    ("state_dir", "relative/dir", "glasses.state_dir:"),
    ("allow_all_users", True, "glasses.allow_all_users: not allowed"),
    ("allow_from", ["*", SECRET], "glasses.allow_from: not allowed"),
    ("dm_policy", "open", "glasses.dm_policy: not allowed"),
]


@pytest.mark.parametrize("key,value,prefix", PLANTED, ids=[f"{k}={v!r}"[:40] for k, v, _ in PLANTED])
def test_real_cli_one_precise_message_no_secret(tmp_path, key, value, prefix):
    code, out, err = cli(write(tmp_path, setter(key, value)), tmp_path)
    errors = [line for line in out.splitlines() if line.startswith("error: ")]
    assert code == admin.EXIT_ERRORS, out + err
    assert len(errors) == 1 and errors[0].startswith(f"error: {prefix}"), out
    assert " — " in errors[0]  # every message carries a hint
    assert SECRET not in out + err


@pytest.mark.parametrize(
    "key,value",
    [
        ("long_threshold", 1),
        ("long_threshold", 512),
        ("host", "::1"),
        ("host", "127.0.0.2"),
        ("public_url", "wss://Glasses.Example.com:443/ws"),
    ],
)
def test_real_cli_accepts_bounds(tmp_path, key, value):
    code, out, _ = cli(write(tmp_path, setter(key, value)), tmp_path)
    assert code == admin.EXIT_OK and out.strip() == "config check: OK (0 error(s), 0 warning(s))", out


def test_real_cli_accepts_32_lanes(tmp_path):
    lanes = [f"l{i}" for i in range(gcfg.MAX_LANES)]
    code, out, _ = cli(write(tmp_path, lambda e, _d: e.update(lanes=lanes, home_lane="l0")), tmp_path)
    assert code == admin.EXIT_OK, out


@pytest.mark.parametrize(
    "env",
    [
        {"GLASSES_ALLOW_ALL_USERS": SECRET},
        {"GLASSES_ALLOWED_USERS": f"*,{SECRET}"},
        {"GATEWAY_ALLOWED_USERS": f"{SECRET},*"},
        {"GATEWAY_ALLOW_ALL_USERS": "yes"},
    ],
)
def test_real_cli_refuses_allow_all_env_without_echo(tmp_path, env):
    code, out, err = cli(EXAMPLE, tmp_path, env)
    errors = [line for line in out.splitlines() if line.startswith("error: environment: ")]
    assert code == admin.EXIT_ERRORS and len(errors) == 1, out
    assert SECRET not in out + err


def test_unknown_keys_warn_without_value_and_exit_0(tmp_path):
    def mutate(extra, _data):
        extra.update(bridge_token=SECRET, long_treshold=100, meeting={"api_key": SECRET})

    code, out, err = cli(write(tmp_path, mutate), tmp_path)
    assert code == admin.EXIT_OK, out
    assert "warning: glasses.long_treshold: unknown key, ignored — did you mean long_threshold?" in out
    assert "warning: glasses.meeting.api_key: unknown key, ignored" in out
    assert SECRET not in out + err


def test_validate_config_logs_never_carry_values(caplog, no_allow_all):
    caplog.set_level(logging.DEBUG)
    extra = {
        "bridge_token": SECRET,
        "allow_from": SECRET,
        "public_url": f"wss://u:{SECRET}@h.example.com/ws",
        "meeting": {"api_key": SECRET},
    }
    assert gcfg.validate_config({"extra": extra}) is False
    assert "glasses.allow_from: not allowed" in caplog.text and "glasses.public_url:" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "meeting",
    [
        {"endpoint": f"https://user:{SECRET}@api.example.com"},
        {"provider": f"sk-{SECRET}"},
        {"llm": {"provider": f"Bearer {SECRET}"}},
    ],
)
def test_meeting_values_that_may_hold_credentials_are_not_echoed(meeting):
    errors = gcfg.check({"meeting": meeting}).errors
    assert len(errors) == 1 and SECRET not in errors[0]


def test_non_string_keys_do_not_crash():
    result = gcfg.check({1: "x", None: 2, ("t",): 3})
    assert result.errors == [] and len(result.warnings) == 3 and result.config is not None


# -- bounds: config, config schema and protocol schema agree ---------------------------------------


def test_lane_bounds_three_way():
    envelope = bounds.envelope_schema()
    lanes = gcfg.schema()["properties"]["lanes"]
    assert gcfg.MAX_LANES == lanes["maxItems"] == bounds.welcome_lanes(envelope)["maxItems"] == 32
    assert gcfg.schema()["$defs"]["lane"]["pattern"] == envelope["$defs"]["lane"]["pattern"]
    assert gcfg.schema()["properties"]["long_threshold"]["maximum"] == gcfg.MAX_LONG_THRESHOLD


def test_readme_contract_test_fails_on_a_mutated_readme(tmp_path, monkeypatch):
    text = (bounds.PROTOCOL_DIR / "README.md").read_text(encoding="utf-8")
    row = "| `msg.out.text` with `longRef` | 512 |"
    assert text.count(row) == 1
    (tmp_path / "README.md").write_text(text.replace(row, "| `msg.out.text` with `longRef` | 511 |"), encoding="utf-8")
    monkeypatch.setattr(bounds, "PROTOCOL_DIR", tmp_path)
    with pytest.raises(AssertionError):
        bounds.test_budget_constants_equal_readme("`msg.out.text` with `longRef`", "MSG_OUT_TEXT_LONGREF_MAX_BYTES")
    (tmp_path / "README.md").write_text(text.replace("## Limits and validation notes", "## Limits"), encoding="utf-8")
    with pytest.raises(IndexError):  # the table vanished: the parser fails loudly, not vacuously
        bounds.readme_limits()


# -- gateway-level keys, --hermes-home, the example config ---------------------------------------


def test_platform_level_host_and_allow_from_are_refused_like_the_gateway(tmp_path):
    def mutate(_extra, data):
        data["gateway"]["platforms"]["glasses"].update(host="0.0.0.0", allow_from=["*"])

    code, out, _ = cli(write(tmp_path, mutate), tmp_path)
    assert code == admin.EXIT_ERRORS, out


def test_hermes_home_flag_reaches_the_hermes_facts(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    (tmp_path / "glasses").mkdir(mode=0o700)
    write(tmp_path)
    seen = []

    def hermes(_cfg):
        seen.append(os.environ.get("HERMES_HOME"))
        return admin.HermesFacts(frozenset({"default", "work"}), {}, {})

    probes = admin.Probes(port_free=lambda _h, _p: True, resolves=lambda _h: True, hermes=hermes)
    admin.main(["config", "check", "--live", "--hermes-home", str(tmp_path)], {}, io.StringIO(), probes)
    assert seen == [str(tmp_path)]


def test_example_is_json_serialisable_and_unchanged_by_load():
    extra = BASE["gateway"]["platforms"]["glasses"]["extra"]
    before = json.dumps(extra, sort_keys=True)
    gcfg.load(extra)
    assert json.dumps(extra, sort_keys=True) == before  # check() never mutates the caller's mapping
