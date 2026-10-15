# SPDX-License-Identifier: Apache-2.0
"""Edge cases of ``glasses-admin config check`` that need no scratch home: credential-looking names and arguments
are never echoed, ``HERMES_HOME`` is set and put back, ``enabled`` is read the way the gateway reads it, and deep
or hostile YAML is one line, never a traceback. Real-Hermes cases run the Hermes venv in a subprocess under
<worktree>/.local/hh-t154."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from glasses import admin
from glasses import hermes_facts as hf
from glasses.config import GlassesConfig

from conftest import PLUGIN_ROOT
from test_config_check_edges import _hermes_run, _live_scratch, _real_live, _scratch
from test_config_check_live_home_exact import CANARY, _tree

# -- credential-looking names are never echoed ------------------------------------------------------------------------

SECRETY = [
    "scheme://KEY" + CANARY,
    "user:" + CANARY + "@host",
    "a@" + CANARY,
    "http:/" + CANARY + "://x",
    "://" + CANARY,
]


def _facts(profile: str, tools: frozenset[str], gate: tuple[str, ...] = ()) -> hf.HermesFacts:
    pf = hf.ProfileFacts("", "", tools, frozenset({"web"}), 5, 0)  # every kind of problem line names the profile
    return hf.HermesFacts(frozenset({profile, "default"}), {"assistant": profile}, {profile: pf, "default": pf}, gate)


@pytest.mark.parametrize("name", SECRETY)
def test_profile_tool_and_gate_names_never_reach_any_output_line(name, tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    glasses = {"public_url": "wss://glasses.example.com/ws", "state_dir": str(tmp_path / "s"), "lanes": ["assistant"]}
    (tmp_path / "s").mkdir(mode=0o700)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"gateway": {"platforms": {"glasses": {"enabled": True, "extra": glasses}}}}), encoding="utf-8"
    )
    for facts in (
        _facts(name, frozenset({name, "web"}), (f"profile {name}: allow-all", f"profile {name!r}: x")),
        _facts("work", frozenset({name}), ()),
    ):
        probes = admin.Probes(
            port_free=lambda *_: True, resolves=lambda _h: True, hermes=lambda _c, f=facts: f, owns_home=lambda _h: True
        )
        out = io.StringIO()
        admin.main(["config", "check", "--live", "--hermes-home", str(home)], {}, out, probes)
        assert "approvals.timeout resolves to 0" in out.getvalue(), out.getvalue()  # the profile lines were printed
        assert CANARY not in out.getvalue(), out.getvalue()


@pytest.mark.parametrize("before", [None, "/was/here", ""])
@pytest.mark.parametrize("error", [None, RuntimeError])
def test_hermes_home_env_sets_the_home_and_puts_back_what_was_there(monkeypatch, before, error, tmp_path):
    if before is None:
        monkeypatch.delenv(hf.HOME_ENV, raising=False)
    else:
        monkeypatch.setenv(hf.HOME_ENV, before)
    snapshot = dict(os.environ)
    with pytest.raises(error) if error else contextlib.nullcontext(), hf.hermes_home_env(tmp_path):
        assert os.environ[hf.HOME_ENV] == str(tmp_path)
        if error:
            raise error
    assert dict(os.environ) == snapshot


@pytest.mark.parametrize("flag", ["--config", "--hermes-home"])
@pytest.mark.parametrize(
    "value", [f"https:///{CANARY}", f"s3://{CANARY}", f"x://u:{CANARY}@h/a", f"://{CANARY}", f"{CANARY}://"]
)
def test_url_shaped_arguments_are_refused_without_the_value(flag, value):
    out = io.StringIO()
    code = admin.main(["config", "check", flag, value], {}, out, admin.DEFAULT_PROBES)
    assert code == admin.EXIT_UNREADABLE and CANARY not in out.getvalue() and len(out.getvalue().splitlines()) == 1


@pytest.mark.parametrize("value", [f"/no/such/{CANARY}@h/config.yaml", f"rel/{CANARY}@x"])
def test_at_sign_in_a_path_argument_is_not_echoed(value):
    out = io.StringIO()
    code = admin.main(["config", "check", "--config", value], {}, out, admin.DEFAULT_PROBES)
    assert code == admin.EXIT_UNREADABLE and CANARY not in out.getvalue()


# -- enabled strings: the copy follows the pinned gateway -------------------------------------------------------------

COERCE_CASES = [
    "yes",
    "YES",
    " on ",
    "On",
    "1",
    "0",
    "y",
    "t",
    "true",
    "True ",
    "false",
    "no",
    "off",
    "",
    " ",
    "none",
    "null",
    "enabled",
    "2",
    "٣",
    "ｙｅｓ",
    "yes\n",
    "\u00a0yes",
    "tru",
    "ye",
]
COERCE_RUNNER = """\
import json, sys
sys.path.insert(0, __PLUGIN_ROOT__)
from gateway.config import _coerce_bool
from glasses.hermes_facts import gateway_enabled
cases = json.loads(sys.argv[1]) + [True, False, 0, 1, 2, 0.0, 0.5, None, [], ["x"], {}, {"a": 1}]
print(json.dumps([[repr(c), _coerce_bool(c, False), gateway_enabled(c)] for c in cases]))
""".replace("__PLUGIN_ROOT__", repr(str(PLUGIN_ROOT)))


def test_gateway_enabled_equals_the_pinned_gateways_coercion_for_odd_inputs():
    with _scratch() as tmp:
        proc = _hermes_run(COERCE_RUNNER, tmp, json.dumps(COERCE_CASES))
    assert proc.returncode == 0, proc.stderr[-2000:]
    rows = json.loads(proc.stdout.strip().splitlines()[-1])
    assert [row for row in rows if row[1] != row[2]] == []


# -- deep or hostile YAML is one line, never a traceback --------------------------------------------------------------


def _run_cli(*argv: str):
    return subprocess.run(
        [sys.executable, "-m", "glasses.admin", *argv],
        cwd=PLUGIN_ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )


DEEP = {
    "flow-seq": "x: " + "[" * 200_000,
    "flow-map": "x: " + "{a: " * 100_000,
    "block-seq": "".join(" " * i + "- \n" for i in range(0, 3000, 1))[:200000],
    "anchor-chain": "a: &a [1]\n" + "".join(f"b{i}: &b{i} [*{'a' if i == 0 else f'b{i - 1}'}]\n" for i in range(200)),
}


@pytest.mark.parametrize("case", DEEP)
def test_deeply_nested_yaml_file_is_one_line_exit_2_in_a_real_process(case, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(DEEP[case], encoding="utf-8")
    proc = _run_cli("config", "check", "--config", str(path))
    assert "Traceback" not in proc.stderr + proc.stdout, (proc.stderr + proc.stdout)[-500:]
    assert proc.returncode in (admin.EXIT_UNREADABLE, admin.EXIT_ERRORS)
    assert len((proc.stdout + proc.stderr).splitlines()) <= 3


def test_deeply_nested_routed_profile_config_does_not_traceback_under_live():
    with _scratch() as tmp:
        root = Path(tmp)
        home, user = _live_scratch(root), root / "user"
        user.mkdir()
        (home / "profiles" / "work" / "config.yaml").write_text("x: " + "[" * 200_000, encoding="utf-8")
        before = _tree(home)
        proc = _real_live(home, user)
        after = _tree(home)
    assert after == before and "Traceback" not in proc.stderr + proc.stdout, (proc.stderr + proc.stdout)[-800:]
    assert proc.returncode == admin.EXIT_ERRORS


def test_glasses_config_class_still_importable_for_probes():
    assert GlassesConfig
