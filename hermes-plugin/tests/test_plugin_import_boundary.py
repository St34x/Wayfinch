# SPDX-License-Identifier: Apache-2.0
"""Importing and registering the plugin never imports Hermes; only the adapter module does, lazily."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

import glasses

from conftest import PLUGIN_ROOT


def pc(extra=None, enabled=True):
    return SimpleNamespace(enabled=enabled, extra=extra)


# --------------------------------------------------------------------------------------------
# importing the package never imports Hermes (fresh interpreter, import blocker)
# --------------------------------------------------------------------------------------------

# Top-level modules/packages of the pinned Hermes tree (hermes-agent @ a4648c5f58): anything named here or
# starting with "hermes" is Hermes.
HERMES_TOP_LEVEL = (
    "acp_adapter",
    "agent",
    "batch_runner",
    "cli",
    "cron",
    "gateway",
    "mcp_serve",
    "mini_swe_runner",
    "model_tools",
    "pm",
    "providers",
    "registration_lifecycle",
    "run_agent",
    "tools",
    "toolsets",
    "toolset_distributions",
    "trajectory_compressor",
    "tui_gateway",
)

_PROBE = textwrap.dedent("""
    import importlib.abc, sys
    HERMES = set({names!r})
    attempts = []

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            top = name.split(".", 1)[0]
            if top in HERMES or top.startswith("hermes"):
                attempts.append(name)
                raise ImportError("blocked Hermes import: " + name)
            return None

    sys.meta_path.insert(0, Block())
    sys.path.insert(0, {root!r})
    import glasses, glasses.config
    from types import SimpleNamespace

    class Ctx:
        def register_platform(self, **kw):
            self.kw = kw

    ctx = Ctx()
    glasses.register(ctx)
    assert ctx.kw["name"] == "glasses"
    glasses.validate_config(SimpleNamespace(enabled=True, extra={{"host": "0.0.0.0"}}))
    glasses.is_connected(SimpleNamespace(enabled=True, extra={{}}))
    glasses.check_requirements()
    glasses.config.load({{}})
    loaded = sorted(m for m in sys.modules if m.split(".", 1)[0] in HERMES or m.startswith("hermes"))
    assert "glasses.adapter" not in sys.modules, "adapter imported eagerly"
    print("ATTEMPTS", attempts)
    print("LOADED", loaded)
""")


def test_import_register_validate_never_import_hermes_in_fresh_interpreter():
    code = _PROBE.format(names=HERMES_TOP_LEVEL, root=str(PLUGIN_ROOT))
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "ATTEMPTS []" in proc.stdout, proc.stdout
    assert "LOADED []" in proc.stdout, proc.stdout


def test_adapter_module_is_the_only_hermes_boundary():
    # adapter.py needs Hermes; in a Hermes-free interpreter importing it fails, while the package works.
    code = (
        f"import sys; sys.path.insert(0, {str(PLUGIN_ROOT)!r})\n"
        "import glasses\n"
        "try:\n"
        "    import glasses.adapter\n"
        "except ImportError as e:\n"
        "    print('IMPORT_ERROR', type(e).__name__)\n"
        "else:\n"
        "    print('IMPORTED')\n"
    )
    proc = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert "IMPORT_ERROR" in proc.stdout, proc.stdout


def test_adapter_factory_imports_lazily():
    call = {}

    class Ctx:
        def register_platform(self, **kw):
            call.update(kw)

    sys.modules.pop("glasses.adapter", None)
    glasses.register(Ctx())
    factory = call["adapter_factory"]
    assert "glasses.adapter" not in sys.modules
    # Calling the factory is what pulls in Hermes; in the dev venv that must fail loudly, not silently.
    with pytest.raises(ImportError):
        factory(pc({}))
    sys.modules.pop("glasses.adapter", None)
