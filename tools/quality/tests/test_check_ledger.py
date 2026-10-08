# SPDX-License-Identifier: Apache-2.0
"""Tests of tools/quality/check_ledger.py: synthetic ledgers, pins and secret-scan scripts in tmp_path, run through
main(). Every planted drift must exit 1 with a `file:line:` finding; anything the gate cannot read exits 2."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

QUALITY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QUALITY))  # check_ledger.py imports ledger_tables.py from its own directory
SHA_A = "a" * 64
SHA_B = "b" * 64


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ledger", QUALITY / "check_ledger.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_ledger"] = module
    spec.loader.exec_module(module)
    return module


cl = _load()

REQS = """\
# Dev toolchain. Direct requirements:
# direct: alpha-lib (runtime)
# direct: Beta_Tool (dev)
# Transitive pins follow.
alpha_lib==1.2.0 \\
    --hash=sha256:aaaa
beta-tool==3.0 \\
    --hash=sha256:bbbb
gamma==0.1 \\
    --hash=sha256:cccc
"""

LEDGER = f"""\
# Dependencies

## Python

| Name | Version | Licence | Scope | Why | Alternative rejected |
|---|---|---|---|---|---|
| Alpha.Lib | 1.2.0 | MIT | runtime | why | alt |
| beta-tool | 3.0 | MIT | dev | why | alt |

## Tool binaries

| Name | Version | Licence | Scope | Why | Alternative rejected |
|---|---|---|---|---|---|
| gitleaks | 8.30.1 | MIT | dev | why | alt |

| Platform | Archive SHA-256 |
|---|---|
| linux_x64 | `{SHA_A}` |
| darwin_arm64 | `{SHA_B}` |

## Licence exceptions

| Name | Licence | Reason it is allowed |
|---|---|---|
| gamma | MPL-2.0 | reason |

No other pin carries a copyleft licence (checked against all
3 pins).
"""

SCAN = f"""\
set -eu

GITLEAKS_VERSION=8.30.1
pinned_sha256() {{
\tcase "$1" in
\tlinux_x64) echo {SHA_A} ;;
\tdarwin_arm64) echo {SHA_B} ;;
\t*) return 1 ;;
\tesac
}}
"""


def plant(root: Path, reqs: str = REQS, ledger: str = LEDGER, scan: str = SCAN) -> Path:
    (root / "docs").mkdir()
    (root / "tools").mkdir()
    (root / "requirements-dev.txt").write_text(reqs, encoding="utf-8")
    (root / "docs" / "dependencies.md").write_text(ledger, encoding="utf-8")
    (root / "tools" / "secret-scan.sh").write_text(scan, encoding="utf-8")
    return root


def run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    rc = cl.main(["--root", str(root)])
    out = capsys.readouterr()
    return rc, out.out, out.err


def replaced(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old
    return text.replace(old, new)


def test_consistent_ledger_passes_with_pep503_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = run(plant(tmp_path), capsys)
    assert (rc, err) == (0, "")
    assert out.startswith("check-ledger: OK (2 direct requirements")
    assert "3 pins" in out and "gitleaks 8.30.1 + 2 digests" in out


def test_extra_direct_pin_without_row_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs = replaced(REQS, "# Transitive", "# direct: delta (dev)\n# Transitive") + "delta==1.0\n"
    ledger = replaced(LEDGER, "all\n3 pins", "all\n4 pins")
    rc, _, err = run(plant(tmp_path, reqs=reqs, ledger=ledger), capsys)
    assert rc == 1
    assert "requirements-dev.txt:4: delta: direct requirement without a row in docs/dependencies.md" in err


def test_extra_transitive_pin_fails_on_stale_pin_count(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(plant(tmp_path, reqs=REQS + "epsilon==2.0\n"), capsys)
    assert rc == 1
    assert "docs/dependencies.md:27: the ledger states all 3 pins, requirements-dev.txt has 4" in err
    assert "has 4: check the licences of the added/removed pins, then update this sentence" in err
    assert "also needs a `# direct: <name> (<scope>)` line in requirements-dev.txt and a ## Python row" in err


def test_row_without_pin_or_direct_line_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = replaced(LEDGER, "| beta-tool |", "| zeta | 1.0 | MIT | dev | why | alt |\n| beta-tool |")
    rc, _, err = run(plant(tmp_path, ledger=ledger), capsys)
    assert rc == 1
    assert "docs/dependencies.md:8: zeta: row without a `# direct:` line in requirements-dev.txt" in err
    assert "docs/dependencies.md:8: zeta: row without a pin in requirements-dev.txt" in err


def test_direct_line_without_pin_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs = replaced(REQS, "beta-tool==3.0 \\\n    --hash=sha256:bbbb\n", "")
    rc, _, err = run(plant(tmp_path, reqs=reqs, ledger=replaced(LEDGER, "3 pins", "2 pins")), capsys)
    assert rc == 1
    assert "requirements-dev.txt:3: Beta_Tool: direct requirement is not pinned" in err
    assert "docs/dependencies.md:8: beta-tool: row without a pin in requirements-dev.txt" in err


def test_version_drift_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(plant(tmp_path, ledger=replaced(LEDGER, "| 1.2.0 |", "| 1.3.0 |")), capsys)
    assert rc == 1
    assert (
        "docs/dependencies.md:7: Alpha.Lib: version 1.3.0 in the ledger, pinned 1.2.0 (requirements-dev.txt:5)" in err
    )


def test_scope_drift_and_unknown_scope_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs = replaced(REQS, "Beta_Tool (dev)", "Beta_Tool (prod)")
    rc, _, err = run(plant(tmp_path, reqs=reqs), capsys)
    assert rc == 1
    assert "requirements-dev.txt:3: Beta_Tool: scope 'prod' is not one of: dev, runtime" in err
    assert "docs/dependencies.md:8: beta-tool: scope dev in the ledger, prod in requirements-dev.txt:3" in err


@pytest.mark.parametrize(
    ("ledger_old", "ledger_new", "finding"),
    [
        ("| 8.30.1 |", "| 8.30.2 |", "docs/dependencies.md:14: gitleaks: version 8.30.2 in the ledger, 8.30.1 in"),
        (f"`{SHA_B}`", f"`{'c' * 64}`", "docs/dependencies.md:19: gitleaks darwin_arm64: digest differs from"),
        (f"| darwin_arm64 | `{SHA_B}` |\n", "", "tools/secret-scan.sh:7: gitleaks darwin_arm64: digest missing from"),
        ("| linux_x64 |", "| linux_arm64 |", "docs/dependencies.md:18: gitleaks linux_arm64: platform not pinned in"),
        ("| gitleaks |", "| trivy |", "docs/dependencies.md:14: trivy: tool binary without a known source of truth"),
        ("| gitleaks |", "| trivy |", "tools/secret-scan.sh:3: gitleaks: no row in docs/dependencies.md"),
    ],
)
def test_gitleaks_drift_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], ledger_old: str, ledger_new: str, finding: str
) -> None:
    rc, _, err = run(plant(tmp_path, ledger=replaced(LEDGER, ledger_old, ledger_new)), capsys)
    assert rc == 1
    assert finding in err


def test_gitleaks_version_bump_in_script_alone_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(plant(tmp_path, scan=replaced(SCAN, "=8.30.1", "=8.31.0")), capsys)
    assert rc == 1
    assert "docs/dependencies.md:14: gitleaks: version 8.30.1 in the ledger, 8.31.0 in tools/secret-scan.sh:3" in err


def test_licence_exception_must_name_a_pin(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(plant(tmp_path, ledger=replaced(LEDGER, "| gamma |", "| omega |")), capsys)
    assert rc == 1
    assert "docs/dependencies.md:25: omega: licence exception names a package that is not pinned" in err


@pytest.mark.parametrize(
    ("old", "new", "finding"),
    [
        ("gamma==0.1 \\", "gamma>=0.1 \\", "requirements-dev.txt:9: malformed pin line (expected <name>==<version>)"),
        ("gamma==0.1 \\", "-r other.txt", "requirements-dev.txt:9: malformed pin line"),
        (
            "# direct: alpha-lib (runtime)",
            "# direct: alpha-lib",
            "requirements-dev.txt:2: malformed direct requirement",
        ),
        ("gamma==0.1", "Alpha.Lib==1.2.0", "requirements-dev.txt:9: Alpha.Lib: duplicate pin (first at line 5)"),
        ("# Transitive", "# direct: ALPHA_lib (runtime)\n#", "requirements-dev.txt:4: ALPHA_lib: duplicate direct"),
    ],
)
def test_malformed_or_duplicate_requirement_lines_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, finding: str
) -> None:
    rc, _, err = run(plant(tmp_path, reqs=replaced(REQS, old, new)), capsys)
    assert rc == 1
    assert finding in err


def test_duplicate_row_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = replaced(LEDGER, "| beta-tool |", "| alpha_lib | 1.2.0 | MIT | runtime | w | a |\n| beta-tool |")
    rc, _, err = run(plant(tmp_path, ledger=ledger), capsys)
    assert rc == 1
    assert "docs/dependencies.md:8: alpha_lib: duplicate row (first at line 7)" in err
