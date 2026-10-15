# SPDX-License-Identifier: Apache-2.0
"""``config check`` and the ``allow_all_users`` key of ``config.yaml`` (top level or under ``gateway:``).

Hermes bridges that key to ``GATEWAY_ALLOW_ALL_USERS`` when the gateway starts, and every allow-all reader of the
gateway sees the result. ``--live`` reads the key the way the bridge does and runs the gate with the bridge applied
(``hermes_facts.allow_all_bridged``): a config that opens the glasses lane to every sender fails the check, with
the same two gate errors as before the plugin stopped calling Hermes' config loaders. The static check does not
read the key (as before). The bridge read is compared with Hermes' own ``bridge_core_env_settings``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from glasses import admin
from glasses import hermes_env as he
from glasses import hermes_facts as hf

from conftest import PLUGIN_ROOT
from test_config_check_edges import _hermes_run, _live_scratch, _real_live, _scratch, live_home

GATE = "GATEWAY_ALLOW_ALL_USERS is enabled and GLASSES_ALLOWED_USERS is empty: the gateway would admit every glasses"
TOP, NESTED = {"allow_all_users": True}, {"gateway": {"allow_all_users": True}}


def _grant(home: Path, patch: dict) -> None:
    config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    for key, value in patch.items():
        config[key] = {**config.get(key, {}), **value} if isinstance(value, dict) else value
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")


def _gate_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("error: allow-all gate: ")]


# -- real Hermes: the grant fails the live check, in both spellings ----------------------------------------------


@pytest.mark.parametrize("patch", [TOP, NESTED], ids=["top-level", "gateway."])
def test_real_live_a_config_granting_every_sender_fails_the_check(patch):
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        control = _real_live(home, user)
        _grant(home, patch)
        proc = _real_live(home, user)
    assert control.returncode == admin.EXIT_OK, control.stdout + control.stderr[-2000:]
    assert proc.returncode == admin.EXIT_ERRORS, proc.stdout + proc.stderr[-2000:]
    assert _gate_lines(proc.stdout) == [
        f"error: allow-all gate: active scope: {GATE} sender; set GLASSES_ALLOWED_USERS",
        f"error: allow-all gate: profile default: {GATE} sender; set GLASSES_ALLOWED_USERS",
    ], proc.stdout
    assert "config check: FAILED (2 error(s), 0 warning(s))" in proc.stdout and proc.stderr == "", proc.stderr


@pytest.mark.parametrize("value", ["true", "True", "yes", "1"])
def test_real_live_every_truthy_spelling_of_the_grant_counts(value):
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _grant(home, {"allow_all_users": value})
        proc = _real_live(home, user)
    assert proc.returncode == admin.EXIT_ERRORS and len(_gate_lines(proc.stdout)) == 2, proc.stdout


@pytest.mark.parametrize("value", [False, "no", "off", "", None, 0])
def test_real_live_a_grant_that_is_off_passes(value):
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _grant(home, {"allow_all_users": value})
        proc = _real_live(home, user)
    assert proc.returncode == admin.EXIT_OK, proc.stdout


def test_real_live_the_top_level_key_wins_over_the_gateway_one():
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _grant(home, {"allow_all_users": False, "gateway": {"allow_all_users": True}})
        proc = _real_live(home, user)
    assert proc.returncode == admin.EXIT_OK, proc.stdout


@pytest.mark.parametrize("dotenv,errors", [("GATEWAY_ALLOW_ALL_USERS=false\n", 0), ("UNRELATED=1\n", 2)])
def test_real_live_an_explicit_variable_in_the_dotenv_wins_over_the_bridge(dotenv, errors):
    with _scratch() as tmp:
        home, user = _live_scratch(Path(tmp)), Path(tmp) / "user"
        user.mkdir()
        _grant(home, TOP)
        (home / ".env").write_text(dotenv, encoding="utf-8")
        proc = _real_live(home, user)
    assert len(_gate_lines(proc.stdout)) == errors, proc.stdout
    assert proc.returncode == (admin.EXIT_ERRORS if errors else admin.EXIT_OK), proc.stdout


@pytest.mark.parametrize("patch", [{}, TOP, NESTED], ids=["no grant", "top-level", "gateway."])
def test_static_check_is_unchanged_it_does_not_read_the_key(tmp_path, patch):
    home = live_home(tmp_path)
    _grant(home, patch)
    out = io.StringIO()
    code = admin.main(["config", "check", "--hermes-home", str(home)], {}, out)
    assert (code, out.getvalue()) == (admin.EXIT_OK, "config check: OK (0 error(s), 0 warning(s))\n")


# -- the read equals Hermes' bridge --------------------------------------------------------------------------------

BRIDGE_RUNNER = f"""\
import json, os, sys
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
from glasses import hermes_env as he
from glasses import hermes_facts as hf
with hf.quarantined_import():
    import gateway.config_loader as cl
out = []
for case, preset in json.load(open(sys.argv[1])):
    for name in ("GATEWAY_ALLOW_ALL_USERS",):
        os.environ.pop(name, None)
    if preset is not None:
        os.environ["GATEWAY_ALLOW_ALL_USERS"] = preset
    mine = he.allow_all_bridge(case)
    cl._BRIDGED_ALLOW_ALL_USERS = False
    cl.bridge_core_env_settings(case, {{}})
    out.append([mine.get("GATEWAY_ALLOW_ALL_USERS"), os.environ.get("GATEWAY_ALLOW_ALL_USERS") if not preset else None,
                cl.bridged_allow_all_users()])
print(json.dumps(out))
"""
BRIDGE_CASES = [
    ({}, None),
    (TOP, None),
    (NESTED, None),
    ({"allow_all_users": False, "gateway": {"allow_all_users": True}}, None),
    ({"allow_all_users": None, "gateway": {"allow_all_users": True}}, None),
    ({"allow_all_users": "Yes"}, None),
    ({"allow_all_users": "on"}, None),
    ({"allow_all_users": 1}, None),
    ({"allow_all_users": "false"}, None),
    ({"gateway": "not a mapping"}, None),
    (TOP, "false"),  # an explicit variable wins
    (TOP, "true"),
]


def test_the_bridge_read_equals_what_hermes_bridges():
    with _scratch() as tmp:
        (Path(tmp) / "cases.json").write_text(json.dumps(BRIDGE_CASES), encoding="utf-8")
        proc = _hermes_run(BRIDGE_RUNNER, tmp, "cases.json")
    assert proc.returncode == 0, proc.stderr[-2000:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert len(result) == len(BRIDGE_CASES)
    for (case, preset), (mine, theirs_env, theirs_flag) in zip(BRIDGE_CASES, result, strict=True):
        if preset:  # the bridge leaves an explicit variable alone, so does ours
            assert mine is None, (case, preset)
        else:
            assert mine == theirs_env == theirs_flag, (case, mine, theirs_env, theirs_flag)
    assert [r[0] for r in result][1:3] == ["true", "true"]  # the cases do distinguish


# -- unit: the pieces --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layers,expected",
    [
        ({"allow_all_users": True}, True),
        ({"gateway": {"allow_all_users": "YES"}}, True),
        ({"allow_all_users": False, "gateway": {"allow_all_users": True}}, False),
        ({"gateway": []}, False),
        (["allow_all_users"], False),
        (None, False),
    ],
)
def test_allow_all_granted_reads_the_two_spellings(layers, expected):
    assert he.allow_all_granted(layers) is expected


def test_the_bridge_leaves_an_explicit_variable_alone(monkeypatch):
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "false")
    assert he.allow_all_bridge(TOP) == {}
    monkeypatch.delenv("GATEWAY_ALLOW_ALL_USERS")
    assert he.allow_all_bridge(TOP) == {"GATEWAY_ALLOW_ALL_USERS": "true"}


@contextlib.contextmanager
def _fake_loader(**attrs):
    """A stand-in ``gateway.config_loader`` in ``sys.modules`` (removed before conftest checks for leaked modules)."""
    loader = SimpleNamespace(**attrs)
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "gateway", SimpleNamespace(config_loader=loader))
        patch.setitem(sys.modules, "gateway.config_loader", loader)
        yield loader


def test_allow_all_bridged_sets_the_variable_and_the_flag_and_puts_both_back():
    with _fake_loader(_BRIDGED_ALLOW_ALL_USERS=False) as loader:
        with pytest.raises(RuntimeError), hf.allow_all_bridged({"GATEWAY_ALLOW_ALL_USERS": "true"}):
            assert os.environ["GATEWAY_ALLOW_ALL_USERS"] == "true" and loader._BRIDGED_ALLOW_ALL_USERS is True
            raise RuntimeError
        assert "GATEWAY_ALLOW_ALL_USERS" not in os.environ and loader._BRIDGED_ALLOW_ALL_USERS is False


def test_allow_all_bridged_with_nothing_to_bridge_touches_nothing():
    with _fake_loader(_BRIDGED_ALLOW_ALL_USERS=True) as loader:
        with hf.allow_all_bridged({}):
            assert "GATEWAY_ALLOW_ALL_USERS" not in os.environ and loader._BRIDGED_ALLOW_ALL_USERS is True
        assert loader._BRIDGED_ALLOW_ALL_USERS is True


def test_allow_all_bridged_removes_a_flag_it_had_to_create():
    with _fake_loader() as loader:
        with hf.allow_all_bridged({"GATEWAY_ALLOW_ALL_USERS": "true"}):
            assert loader._BRIDGED_ALLOW_ALL_USERS is True
        assert not hasattr(loader, "_BRIDGED_ALLOW_ALL_USERS")
