# SPDX-License-Identifier: Apache-2.0
"""Import-boundary gate: bypasses of the core purity rules.

Planted code goes into the gate's own scratch core module (imports_scratch.py: a scratch copy of
``hermes-plugin/glasses`` with the real ``.importlinter``); a case passes only when lint-imports OR
check_core_purity.py fails it (the gate runs both). Every test plants its own state.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imports_scratch as sc

SCRIPT = Path(__file__).resolve().parents[1] / "check_core_purity.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_core_purity_bypass_mod", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cp = _load()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return sc.make_tree(tmp_path)


def _gate_fails(root: Path) -> bool:
    lint = sc.lint(root)
    purity = cp.main(["--config", str(root / ".importlinter"), "--src", str(root / "hermes-plugin")])
    return lint.returncode != 0 or purity != 0


def test_unplanted_copy_passes(tree: Path) -> None:
    assert not _gate_fails(tree)


@pytest.mark.parametrize(
    "source",
    [
        "import urllib.request\n",
        "from urllib import request\n",
        "_S = __import__('socket')\n",
        "exec('import socket')\n",
        "def _r() -> str:\n    with open('x') as f:\n        return f.read()\n",
        "import os.path as _p\n",
        "from importlib import import_module\n",
        "import tempfile\n",
        "import concurrent.futures\n",
    ],
)
def test_planted_io_in_core_fails(tree: Path, source: str) -> None:
    sc.plant(tree, source)
    assert _gate_fails(tree)


def test_empty_core_list_is_a_config_error(tree: Path) -> None:
    sc.set_values(tree / ".importlinter", "source_modules", [])
    assert _gate_fails(tree)
    assert cp.main(["--config", str(tree / ".importlinter"), "--src", str(tree / "hermes-plugin")]) == 2


def test_ignore_entry_without_id_comment_fails(tree: Path) -> None:
    sc.plant(tree, "from .registry import Registry\n")
    sc.add_ignore(tree / ".importlinter", f"{sc.CORE} -> glasses.registry", comment=None)
    text = (tree / ".importlinter").read_text(encoding="utf-8")
    assert f"ignore_imports =\n    {sc.CORE} -> glasses.registry\n" in text  # no id comment above the entry
    assert _gate_fails(tree)


@pytest.mark.parametrize(
    "source",
    [
        "import io\n",
        "from io import open as _open\n",
        "import codecs\n",
        "def _w() -> None:\n    import io\n    io.open('x')\n",
        "import sys\n",
        "import logging\n",
        "import requests\n",
        "import httpx\n",
        "from datetime import datetime\n_T = datetime.now()\n",
    ],
)
def test_file_console_clock_and_third_party_io_fails(tree: Path, source: str) -> None:
    sc.plant(tree, source)
    assert _gate_fails(tree)
