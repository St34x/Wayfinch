# SPDX-License-Identifier: Apache-2.0
"""tools/quality/check_ledger.py over the real repository: the ledger, the pins and the secret scan agree."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHECKER = ROOT / "tools" / "quality" / "check_ledger.py"


def test_real_tree_ledger_matches_pins_and_secret_scan() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(ROOT)], capture_output=True, text=True, check=False
    )
    assert (result.returncode, result.stderr) == (0, "")
    assert result.stdout.startswith("check-ledger: OK (")
    assert "gitleaks 8.30.1 + 4 digests" in result.stdout


def test_fragment_wires_the_gate_into_make_test() -> None:
    fragment = (ROOT / "mk" / "ledger.mk").read_text(encoding="utf-8").splitlines()
    assert fragment[0] == "# SPDX-License-Identifier: Apache-2.0"
    assert "TEST_TARGETS += check-ledger" in fragment
    assert any(line.startswith("#: check-ledger — ") for line in fragment)
