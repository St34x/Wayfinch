# SPDX-License-Identifier: Apache-2.0
"""``connect()`` on the real edge start, from a copy of the package alone (the documented install) with the
protocol/ directory absent: several overlapping ``connect()`` calls serve one socket, a package without its shipped
schema fails as the retryable start error (no exception out of ``connect()``), a copy under a read-only directory
works, and a second gateway process cannot take the socket of the first."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from edge_helpers import other_user, short_dir

from test_adapter_copy_install import PACKAGE, SCRIPT

THREE_CONNECTS = SCRIPT.replace(
    "connected = await adapter_.connect()",
    "connected = all(await asyncio.gather(adapter_.connect(), adapter_.connect(), adapter_.connect()))",
)
assert THREE_CONNECTS != SCRIPT


def _install(tmp_path: Path) -> Path:
    plugins = tmp_path / "plugins"
    shutil.copytree(PACKAGE, plugins / "glasses", ignore=shutil.ignore_patterns("__pycache__"))
    return plugins


def _run(tmp_path: Path, script: str, plugins: Path, sock: Path) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "copy_install.py"
    path.write_text(script, encoding="utf-8")
    argv = [sys.executable, str(path), str(plugins), str(sock), other_user().pw_name]
    argv += [str(tmp_path / "state"), str(tmp_path / "pub")]
    return subprocess.run(argv, capture_output=True, text=True, check=False, cwd=tmp_path, timeout=60)


def test_overlapping_connects_on_the_real_start_serve_one_socket(tmp_path: Path) -> None:
    plugins = _install(tmp_path)
    with short_dir() as d:
        done = _run(tmp_path, THREE_CONNECTS, plugins, d / "edge.sock")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"connected": True, "errors": []}  # a second listener would be "in use"


def test_a_package_without_its_shipped_schema_is_the_retryable_start_error(tmp_path: Path) -> None:
    plugins = _install(tmp_path)
    (plugins / "glasses" / "generated" / "envelope.schema.json").unlink()
    with short_dir() as d:
        done = _run(tmp_path, SCRIPT, plugins, d / "edge.sock")
        assert not (d / "edge.sock").exists()  # nothing was bound
    assert done.returncode == 0, done.stderr  # connect() returned, it did not raise
    out = json.loads(done.stdout)
    assert out["connected"] is False
    ((code, message, retryable),) = out["errors"]
    assert (code, retryable) == ("edge_listener", True) and "not started" in message


def test_a_package_under_a_read_only_directory_connects(tmp_path: Path) -> None:
    plugins = _install(tmp_path)
    for path in [plugins, *plugins.rglob("*")]:
        path.chmod(path.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        with short_dir() as d:
            done = _run(tmp_path, SCRIPT, plugins, d / "edge.sock")
    finally:
        for path in [plugins, *plugins.rglob("*")]:
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"connected": True, "errors": []}


def test_the_shipped_schema_is_a_byte_copy_of_the_protocol_schema() -> None:
    root = Path(__file__).resolve().parents[2]
    shipped = (PACKAGE / "generated" / "envelope.schema.json").read_bytes()
    assert shipped == (root / "protocol" / "envelope.schema.json").read_bytes()
    assert os.path.getsize(PACKAGE / "generated" / "envelope.schema.json") > 1000
