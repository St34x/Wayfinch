# SPDX-License-Identifier: Apache-2.0
"""The plugin installed by copying ``hermes-plugin/glasses/`` alone (the documented install): no ``protocol/``
directory beside it,
yet ``connect()`` finds the envelope schema (it ships inside the package, ``generated/envelope.schema.json``) and
serves the edge socket. The check runs in a subprocess against stand-in ``gateway.*`` modules, so the copy is
imported from the scratch directory and nothing of the repository checkout is on its path."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from edge_helpers import other_user, short_dir

PACKAGE = Path(__file__).resolve().parents[1] / "glasses"

SCRIPT = """
import asyncio, json, sys, types

root, sock, user, state, signer_dir = sys.argv[1:6]
sys.path.insert(0, root)


class Base:
    def __init__(self, config, platform):
        self.fatal_errors = []

    def _set_fatal_error(self, code, message, *, retryable):
        self.fatal_errors.append([code, message, retryable])

    def _mark_disconnected(self):
        pass


names = ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base", "gateway.platforms.event")
modules = {name: types.ModuleType(name) for name in names}
modules["gateway.config"].Platform = lambda value: value
modules["gateway.platforms.base"].BasePlatformAdapter = Base
modules["gateway.platforms.base"].SendResult = dict
modules["gateway.platforms.event"].MessageEvent = dict
modules["gateway.platforms.event"].MessageType = types.SimpleNamespace(TEXT="text")
sys.modules.update(modules)

from glasses import adapter

assert adapter.__file__.startswith(root), adapter.__file__
extra = {
    "edge": {"socket": sock, "user": user},
    "signer": {"user": user, "public_dir": signer_dir, "socket": "/run/device-signer/signer.sock"},
    "state_dir": state,
}
adapter_ = adapter.GlassesAdapter(types.SimpleNamespace(enabled=True, extra=extra))


async def run():
    connected = await adapter_.connect()
    await adapter_.disconnect()
    return connected


print(json.dumps({"connected": asyncio.run(run()), "errors": adapter_.fatal_errors}))
"""


def test_a_copy_of_the_package_alone_loads_the_schema_and_connects(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    shutil.copytree(PACKAGE, plugins / "glasses", ignore=shutil.ignore_patterns("__pycache__"))
    assert not (tmp_path / "protocol").exists() and not (plugins / "protocol").exists()
    script = tmp_path / "copy_install.py"
    script.write_text(SCRIPT, encoding="utf-8")
    with short_dir() as d:
        argv = [sys.executable, str(script), str(plugins), str(d / "edge.sock"), other_user().pw_name]
        argv += [str(tmp_path / "state"), str(tmp_path / "pub")]
        done = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=tmp_path, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"connected": True, "errors": []}
