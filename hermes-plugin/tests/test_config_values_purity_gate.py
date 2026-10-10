# SPDX-License-Identifier: Apache-2.0
"""The core purity gate covers ``glasses.config_values``: I/O planted into a scratch copy of the real file fails the
gate (exit 1), the unmodified copy passes, and its absolute-path rule equals ``posixpath.isabs`` for clean strings.
"""

from __future__ import annotations

import importlib.util
import posixpath
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from glasses import config_values

from conftest import PLUGIN_DIR, PLUGIN_ROOT

TOP = PLUGIN_ROOT.parent
GATE = TOP / "tools" / "quality" / "check_core_purity.py"


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("core_purity_gate_for_config_values", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scratch(tmp_path: Path, plant: str) -> list[str]:
    src = tmp_path / "hermes-plugin"
    shutil.copytree(PLUGIN_DIR, src / "glasses", ignore=shutil.ignore_patterns("__pycache__"))
    target = src / "glasses" / "config_values.py"
    target.write_text(target.read_text(encoding="utf-8") + plant, encoding="utf-8")
    return ["--config", str(TOP / ".importlinter"), "--src", str(src)]


@pytest.mark.parametrize(
    "plant",
    [
        "import socket\n",
        "import os\n",
        "import os.path\n",
        "from os import path as _p\n",
        "import posixpath\n",
        "import io\n",
        "\n\ndef _leak() -> str:\n    with open('/x', encoding='utf-8') as fh:\n        return fh.read()\n",
        "\n\ndef _imp() -> object:\n    return __import__('socket')\n",
    ],
    ids=["socket", "os", "os-path", "from-os", "posixpath", "io", "open", "dunder-import"],
)
def test_io_planted_into_config_values_fails_the_core_purity_gate(
    tmp_path: Path, plant: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _gate().main(_scratch(tmp_path, plant)) == 1
    assert "config_values.py" in capsys.readouterr().err


def test_unmodified_copy_of_config_values_passes_the_core_purity_gate(tmp_path: Path) -> None:
    assert _gate().main(_scratch(tmp_path, "")) == 0


@pytest.mark.parametrize(
    "value",
    [
        "/",
        "/a",
        "//a",
        "///a",
        "/\u00e9t\u00e9",
        "/a/../b",
        "/.",
        "a/",
        ".",
        "..",
        "\\a",
        "C:\\a",
        "\u2215a",
        "\uff0fa",
    ],
)
def test_absolute_path_rule_equals_posixpath_isabs_for_clean_strings(value: str) -> None:
    assert config_values._empty_or_abs_path(value) is posixpath.isabs(value)
