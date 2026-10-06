# SPDX-License-Identifier: Apache-2.0
"""mypy --strict gate: untyped code that check_mypy_config.py and mypy once both let through.

Each case plants an untyped function in a small scratch tree (the real `[mypy]` section, an empty `glasses` package,
one typed anchor under tools/) and runs the gate: check_mypy_config.py, then mypy with the gate's argv. A small tree
keeps every mypy run to seconds; the behaviours probed (stub resolution, reachability, the directory crawl) do not
depend on the rest of the tree. Every bypass is closed; the cases stay as regression tests.
"""

import subprocess
import sys
from pathlib import Path

from test_mypy_baseline import ROOT, cmc, run_mypy

UNTYPED = "def {name}(a, b):\n    return a + b\n"
GLOBAL_ONLY = "[mypy]\n" + "".join(f"{key} = {value}\n" for key, value in cmc.GLOBAL.items())


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "hermes-plugin/glasses").mkdir(parents=True)
    (root / "hermes-plugin/glasses/__init__.py").write_text("", encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools/planted_anchor.py").write_text(
        "def anchor(value: int) -> int:\n    return value\n", encoding="utf-8"
    )
    (root / "mypy.ini").write_text(GLOBAL_ONLY, encoding="utf-8")
    return root


def _gate(root: Path) -> tuple[int, int, str]:
    """(check_mypy_config exit, mypy exit, mypy output) on the scratch tree `root`."""
    config = cmc.main(["--root", str(root)])
    code, output = run_mypy(root, root / "mypy.ini", root.parent / "cache")
    return config, code, output


def test_the_scratch_gate_is_green_and_sees_a_plain_untyped_function(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert _gate(root)[:2] == (0, 0)
    (root / "hermes-plugin/glasses/planted_plain.py").write_text(UNTYPED.format(name="planted_plain"), encoding="utf-8")
    _, code, output = _gate(root)
    assert code == 1 and "planted_plain.py:1: error: Function is missing a type annotation" in output, output


def test_stub_next_to_an_untyped_module_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    package = root / "hermes-plugin/glasses"
    (package / "planted_stubbed.py").write_text(UNTYPED.format(name="planted_stubbed"), encoding="utf-8")
    (package / "planted_stubbed.pyi").write_text("def planted_stubbed(a: int, b: int) -> int: ...\n", encoding="utf-8")
    config, code, output = _gate(root)
    assert config != 0 or code != 0, output


def test_untyped_code_under_not_type_checking_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    body = "".join("    " + line for line in UNTYPED.format(name="planted_runtime_only").splitlines(keepends=True))
    source = "from typing import TYPE_CHECKING\n\nif not TYPE_CHECKING:\n\n" + body
    (root / "hermes-plugin/glasses/planted_runtime_only.py").write_text(source, encoding="utf-8")
    config, code, output = _gate(root)
    assert config != 0 or code != 0, output


def test_untyped_code_in_a_hidden_tools_dir_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "tools/.planted").mkdir()
    (root / "tools/.planted/planted_hidden.py").write_text(UNTYPED.format(name="planted_hidden"), encoding="utf-8")
    config, code, output = _gate(root)
    assert config != 0 or code != 0, output


def test_a_root_pyproject_or_setup_cfg_cannot_weaken_the_gate(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "pyproject.toml").write_text("[tool.mypy]\nignore_errors = true\n", encoding="utf-8")
    (root / "setup.cfg").write_text("[mypy]\nignore_errors = True\n", encoding="utf-8")
    (root / ".mypy.ini").write_text("[mypy]\nignore_errors = True\n", encoding="utf-8")
    (root / "hermes-plugin/glasses/planted_cfg.py").write_text(UNTYPED.format(name="planted_cfg"), encoding="utf-8")
    _, code, output = _gate(root)
    assert code == 1 and "planted_cfg.py:1: error: Function is missing a type annotation" in output, output


def test_non_utf8_checked_file_fails_closed(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "tools/planted_latin1.py").write_bytes(b"# -*- coding: latin-1 -*-\nX = '\xe9'\n")
    argv = [sys.executable, str(ROOT / "tools/quality/check_mypy_config.py"), "--root", str(root)]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert done.returncode == 2, done.stdout
    assert "Traceback" not in done.stderr
    assert "check-mypy-config: cannot read a checked file: tools/planted_latin1.py: not UTF-8" in done.stderr, (
        done.stderr
    )
