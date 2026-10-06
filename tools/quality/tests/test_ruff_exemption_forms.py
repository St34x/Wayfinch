# SPDX-License-Identifier: Apache-2.0
"""ruff gate: exemption forms the bypass scan of test_ruff_bypass.py does not see.

Each case runs the gate's own ``ruff check --config ruff.toml`` on stdin. Both forms
are caught; the cases stay as regression tests.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from test_ruff_bypass import file_exemptions

ROOT = Path(__file__).resolve().parents[3]
FIVE_PARAMS = "def f(a, b, c, d, e):\n    return a + b + c + d + e\n"
# Built from parts so that this file holds no such comment itself.
TRAILING = "# header text  # ruff" + ": noqa: PLR0913, PLR0917\n"


def _gate(source: str, filename: str) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, "-m", "ruff", "check", "--no-cache", "--config", "ruff.toml", "--stdin-filename", filename]
    return subprocess.run([*argv, "-"], cwd=ROOT, input=source, capture_output=True, text=True, check=False)


def test_ruff_honours_a_file_exemption_after_other_comment_text() -> None:
    # The bypass itself: a comment-only line whose comment ENDS with the file-level exemption switches the rules off.
    assert _gate(FIVE_PARAMS, "hermes-plugin/glasses/_probe.py").returncode == 1
    assert _gate(TRAILING + FIVE_PARAMS, "hermes-plugin/glasses/_probe.py").returncode == 0


def test_scan_finds_a_file_exemption_after_other_comment_text() -> None:
    assert file_exemptions(TRAILING + FIVE_PARAMS) == [1]


def test_file_exemption_after_code_is_not_honoured() -> None:
    # Control: after code on the same line ruff only warns and still reports, so the scan may ignore that form.
    done = _gate("X = 1  # see docs # flake8" + ": noqa: PLR0913\n" + FIVE_PARAMS, "hermes-plugin/glasses/_probe.py")
    assert done.returncode == 1 and "PLR0913" in done.stdout


def test_hand_written_module_in_another_generated_dir_is_linted() -> None:
    # check-codegen owns only hermes-plugin/glasses/generated; a module in any other generated/ dir is hand-written.
    done = _gate(FIVE_PARAMS, "hermes-plugin/glasses/lanes/generated/helpers.py")
    assert done.returncode == 1 and "PLR0913" in done.stdout


def test_the_owned_generated_dir_stays_excluded() -> None:
    assert _gate(FIVE_PARAMS, "hermes-plugin/glasses/generated/protocol_types.py").returncode == 0
