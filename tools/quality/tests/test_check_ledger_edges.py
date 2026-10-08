# SPDX-License-Identifier: Apache-2.0
"""Fail-closed tests of tools/quality/check_ledger.py: whatever the gate cannot read or find exits 2, never OK."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_check_ledger import LEDGER, REQS, SCAN, cl, plant, replaced, run


def test_missing_files_cannot_be_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for name in ("requirements-dev.txt", "docs/dependencies.md", "tools/secret-scan.sh"):
        root = tmp_path / name.replace("/", "_")
        root.mkdir()
        plant(root)
        (root / name).unlink()
        rc, out, err = run(root, capsys)
        assert (rc, out) == (2, ""), name
        assert f"check-ledger: cannot check: {name}: cannot read" in err


def test_not_utf8_cannot_be_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plant(tmp_path)
    (tmp_path / "docs" / "dependencies.md").write_bytes(b"# Dependencies\n\xff\xfe\n")
    rc, _, err = run(tmp_path, capsys)
    assert rc == 2
    assert "docs/dependencies.md: cannot read" in err


@pytest.mark.parametrize(
    ("reqs", "message"),
    [
        ("# no direct lines\nalpha_lib==1.2.0\n", "requirements-dev.txt: no `# direct: <name> (<scope>)` line"),
        ("# direct: alpha-lib (runtime)\n", "requirements-dev.txt: no `<name>==<version>` pin"),
    ],
)
def test_empty_requirement_lists_cannot_be_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], reqs: str, message: str
) -> None:
    rc, _, err = run(plant(tmp_path, reqs=reqs), capsys)
    assert rc == 2
    assert message in err


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("## Python", "## Python packages", "docs/dependencies.md: section '## Python' not found"),
        ("Python\n\n| Name | Version |", "Python\n\n| Name | Release |", "no table with a 'Version'"),
        (
            "Python\n\n| Name | Version | Licence | Scope |",
            "Python\n\n| Name | Version | Licence | Kind |",
            "no 'Scope'",
        ),
        ("| Platform | Archive SHA-256 |", "| Platform | Digest |", "no 'Archive SHA-256' column"),
        ("| gamma | MPL-2.0 | reason |\n", "", "'## Licence exceptions' table has no rows"),
        ("all\n3 pins", "every pin", "docs/dependencies.md: no 'all <N> pins' statement"),
    ],
)
def test_missing_ledger_structure_cannot_be_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, message: str
) -> None:
    rc, _, err = run(plant(tmp_path, ledger=replaced(LEDGER, old, new)), capsys)
    assert rc == 2
    assert message in err


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("GITLEAKS_VERSION=8.30.1\n", "", "tools/secret-scan.sh: no GITLEAKS_VERSION="),
        (f"\tlinux_x64) echo {'a' * 64} ;;\n\tdarwin_arm64) echo {'b' * 64} ;;\n", "", "no `<platform>) echo <sha256>"),
        ("\tesac\n", "", 'tools/secret-scan.sh: no `pinned_sha256() {` + `case "$1" in` ... `esac` block'),
        ('\tcase "$1" in\n', "", 'tools/secret-scan.sh: no `pinned_sha256() {` + `case "$1" in`'),
        ("pinned_sha256() {\n", "digest() {\n", "tools/secret-scan.sh: no `pinned_sha256() {`"),
    ],
)
def test_missing_scan_pins_cannot_be_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, message: str
) -> None:
    rc, _, err = run(plant(tmp_path, scan=replaced(SCAN, old, new)), capsys)
    assert rc == 2
    assert message in err


def test_comment_and_continuation_lines_are_not_pins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs = REQS + "\n# a comment: x==1\nomega==1.0 \\\n    --hash=sha256:4444 \\\n    --hash=sha256:5555\n   \n"
    ledger = replaced(LEDGER, "all\n3 pins", "all\n4 pins")
    rc, _, err = run(plant(tmp_path, reqs=reqs, ledger=ledger), capsys)
    assert (rc, err) == (0, "")


def test_findings_go_to_stderr_one_per_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = replaced(replaced(LEDGER, "| 1.2.0 |", "| 9 |"), "| gamma |", "| omega |")
    rc, out, err = run(plant(tmp_path, ledger=ledger), capsys)
    assert (rc, out) == (1, "")
    lines = err.splitlines()
    assert lines[-1] == "check-ledger: 2 finding(s)"
    assert all(line.startswith("docs/dependencies.md:") for line in lines[:-1])
    assert cl.norm("Mypy_Extensions") == cl.norm("mypy-extensions") == "mypy-extensions"
