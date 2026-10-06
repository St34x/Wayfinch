# SPDX-License-Identifier: Apache-2.0
"""Python floor of the mypy gate (T1.133, ENGINEERING.md section 8): code must type-check for Python 3.11.

The plugin runs inside the Hermes venv (Python 3.11 and newer); the dev venv is newer. With `python_version = 3.11`
mypy reads the typeshed stubs of 3.11, so a stdlib API added later fails, and it rejects syntax added later. Each case
plants a module in a small scratch tree (the real `[mypy]` section only) and runs the gate: check_mypy_config.py,
then mypy with the gate's argv.
"""

from pathlib import Path

import pytest

from test_mypy_baseline import cmc, run_mypy

GLOBAL_ONLY = "[mypy]\n" + "".join(f"{key} = {value}\n" for key, value in cmc.GLOBAL.items())
BATCHED = (
    "import itertools\n\n\ndef pairs(items: list[int]) -> list[tuple[int, ...]]:\n"
    "    return list(itertools.batched(items, 2))\n"
)
EXCEPT_314 = (
    "def parse(text: str) -> int:\n    try:\n        return int(text)\n"
    "    except ValueError, TypeError:\n        return 0\n"
)


def _gate(tmp_path: Path, planted: str) -> tuple[int, int, str]:
    """(check_mypy_config exit, mypy exit, mypy output) on a scratch tree holding `planted` as a plugin module."""
    root = tmp_path / "tree"
    (root / "hermes-plugin/glasses").mkdir(parents=True)
    (root / "hermes-plugin/glasses/__init__.py").write_text("", encoding="utf-8")
    (root / "hermes-plugin/glasses/floor_planted.py").write_text(planted, encoding="utf-8")
    (root / "tools").mkdir()
    (root / "tools/floor_anchor.py").write_text("def anchor(value: int) -> int:\n    return value\n", encoding="utf-8")
    (root / "mypy.ini").write_text(GLOBAL_ONLY, encoding="utf-8")
    code, output = run_mypy(root, root / "mypy.ini", tmp_path / "cache")
    return cmc.main(["--root", str(root)]), code, output


def test_the_floor_is_the_oldest_python_hermes_supports() -> None:
    assert cmc.GLOBAL["python_version"] == "3.11"


def test_scratch_gate_is_green_with_3_11_code(tmp_path: Path) -> None:
    assert _gate(tmp_path, "import itertools\n\nPAIRS = list(itertools.pairwise([1, 2, 3]))\n")[:2] == (0, 0)


@pytest.mark.parametrize(
    ("planted", "message"),
    [
        (BATCHED, 'floor_planted.py:5: error: Module has no attribute "batched"'),  # stdlib API of 3.12
        (EXCEPT_314, "floor_planted.py:4: error: Multiple exception types must be parenthesized"),  # syntax of 3.14
    ],
)
def test_code_newer_than_the_floor_fails(tmp_path: Path, planted: str, message: str) -> None:
    _, code, output = _gate(tmp_path, planted)
    assert code != 0 and message in output, output
