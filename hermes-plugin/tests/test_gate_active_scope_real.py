# SPDX-License-Identifier: Apache-2.0
"""The allow-all gate against real Hermes: the active secret scope of the caller's context is part of the verdict key.

Runs the real Hermes venv in a subprocess with the real ``platform_gate_env`` and ``set_secret_scope`` (a context
variable): a context whose scope holds a gate switch must be refused inside the second in which a clean context was
judged. The value is a sentinel that must not appear in any output.
"""

from __future__ import annotations

import json
import subprocess

from conftest import PLUGIN_ROOT
from test_config_check_edges import _scratch
from test_config_check_live import hermes_python
from test_gate_served_profiles_real import SENTINEL, _home

RUNNER = f"""\
import contextvars, json, os, sys
sys.path.insert(0, {str(PLUGIN_ROOT)!r})
import gateway.platforms._shared  # noqa: F401 - the marker that tells the gate Hermes is loaded
from agent.secret_scope import set_multiplex_context, set_secret_scope
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from glasses import config as gcfg
from glasses.adapter import GlassesAdapter

platform_registry.register(PlatformEntry(name="glasses", label="Glasses", adapter_factory=lambda c: None,
    check_fn=lambda: True, validate_config=gcfg.validate_config, required_env=[], install_hint="", source="plugin"))

set_multiplex_context(True)  # platform_gate_env reads the installed scope only while multiplexing
adapter = GlassesAdapter(PlatformConfig(enabled=True, extra={{"public_url": "wss://glasses.example.com/ws",
                                                            "lanes": ["assistant", "work"]}}))

def with_scope(values):
    def run():
        set_secret_scope(values)
        return adapter.gate_ok()
    return contextvars.copy_context().run(run)

out = {{"clean": with_scope({{}})}}
out["switched"] = with_scope({{"GLASSES_ALLOW_ALL_USERS": {SENTINEL!r}}})
out["clean_again"] = with_scope({{}})
out["calls"] = [with_scope({{"GATEWAY_ALLOW_ALL_USERS": "true"}}) for _ in range(3)]
print(json.dumps(out))
"""


def test_real_hermes_a_context_with_a_switch_in_its_scope_is_refused_inside_the_second_of_a_clean_one():
    with _scratch() as tmp:
        home, user = _home(tmp)
        script = home.parent / "runner.py"
        script.write_text(RUNNER, encoding="utf-8")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(user), "HERMES_HOME": str(home)}
        proc = subprocess.run(
            [str(hermes_python()), str(script)],
            cwd=home.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert SENTINEL not in proc.stdout + proc.stderr, "a scope value reached the output"
    result = json.loads(proc.stdout.splitlines()[-1])
    assert result == {"clean": True, "switched": False, "clean_again": True, "calls": [False] * 3}, result
