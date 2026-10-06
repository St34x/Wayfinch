# SPDX-License-Identifier: Apache-2.0
"""tools/codegen is a script directory, not a package: put it on sys.path before pytest imports the test modules,
so they import gen_protocol / protocol_envelope / protocol_model at the top like any module (mypy resolves them from
the file paths in its scope)."""

import sys
from pathlib import Path

CODEGEN = Path(__file__).resolve().parents[1]
if str(CODEGEN) not in sys.path:
    sys.path.insert(0, str(CODEGEN))
