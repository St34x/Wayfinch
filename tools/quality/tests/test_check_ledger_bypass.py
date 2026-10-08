# SPDX-License-Identifier: Apache-2.0
"""Bypass probes of tools/quality/check_ledger.py: requirement forms the gate must not take as exact pins, header
variants, every-digest tampering, and statements it must not silently skip."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_check_ledger import LEDGER, REQS, SCAN, SHA_A, SHA_B, plant, replaced, run


@pytest.mark.parametrize(
    "line",
    [
        "alpha_lib[extra]==1.2.0 \\",
        'alpha_lib==1.2.0; python_version >= "3.11" \\',
        "alpha_lib==1.2.0  # via something \\",
        "alpha_lib>=1.2.0 \\",
        "alpha_lib === 1.2.0 \\",
    ],
)
def test_non_exact_pin_forms_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str) -> None:
    rc, out, err = run(plant(tmp_path, reqs=replaced(REQS, "alpha_lib==1.2.0 \\", line)), capsys)
    assert (rc, out) == (1, "")
    assert "requirements-dev.txt:5: malformed pin line" in err


def test_include_and_option_lines_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(plant(tmp_path, reqs=REQS + "-r other.txt\n--index-url https://example.com/simple\n"), capsys)
    assert rc == 1
    assert "requirements-dev.txt:11: malformed pin line" in err
    assert "requirements-dev.txt:12: malformed pin line" in err


@pytest.mark.parametrize("header", ["# Direct: Beta_Tool (dev)", " # direct: Beta_Tool (dev)", "# direct Beta_Tool"])
def test_direct_header_variants_do_not_count_as_direct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], header: str
) -> None:
    rc, _, err = run(plant(tmp_path, reqs=replaced(REQS, "# direct: Beta_Tool (dev)", header)), capsys)
    assert rc == 1
    assert "beta-tool: row without a `# direct:` line" in err or "malformed direct requirement line" in err


def test_every_ledger_digest_tampered_fails_per_platform(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = replaced(replaced(LEDGER, SHA_A, "0" * 64), SHA_B, "1" * 64)
    rc, _, err = run(plant(tmp_path, ledger=ledger), capsys)
    assert rc == 1
    assert "gitleaks linux_x64: digest differs" in err
    assert "gitleaks darwin_arm64: digest differs" in err


def test_every_script_digest_tampered_fails_per_platform(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scan = replaced(replaced(SCAN, SHA_A, "0" * 64), SHA_B, "1" * 64)
    rc, _, err = run(plant(tmp_path, scan=scan), capsys)
    assert rc == 1
    assert err.count("digest differs from tools/secret-scan.sh") == 2


def test_upper_case_digest_is_not_equal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(plant(tmp_path, ledger=replaced(LEDGER, SHA_B, SHA_B.upper())), capsys)
    assert rc == 1
    assert "gitleaks darwin_arm64: digest differs" in err


def test_capitalised_pin_count_statement_cannot_be_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, out, err = run(plant(tmp_path, ledger=replaced(LEDGER, "all\n3 pins", "All\n3 pins")), capsys)
    assert (rc, out) == (2, "")
    assert "no 'all <N> pins' statement" in err


def test_ledger_path_that_is_a_directory_cannot_be_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plant(tmp_path)
    (tmp_path / "docs" / "dependencies.md").unlink()
    (tmp_path / "docs" / "dependencies.md").mkdir()
    rc, out, err = run(tmp_path, capsys)
    assert (rc, out) == (2, "")
    assert "docs/dependencies.md: cannot read" in err


def test_crlf_requirements_and_ledger_are_read_like_lf(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reqs, ledger = (text.replace("\n", "\r\n") for text in (REQS, LEDGER))
    rc, out, _ = run(plant(tmp_path, reqs=reqs, ledger=ledger), capsys)
    assert rc == 0
    assert out.startswith("check-ledger: OK (2 direct requirements")


def test_crlf_secret_scan_script_cannot_be_checked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """sh does not strip a CR before LF: `esac<CR>` is no `esac`, so the digest table cannot be read (exit 2)."""
    rc, out, err = run(plant(tmp_path, scan=SCAN.replace("\n", "\r\n")), capsys)
    assert (rc, out) == (2, "")
    assert 'tools/secret-scan.sh: no `pinned_sha256() {` + `case "$1" in` ... `esac` block' in err


@pytest.mark.xfail(
    strict=True,
    reason="known gap: directness is declared, not derived from the pin graph; a direct dependency whose `# direct:` "
    "line and row are both removed looks transitive, and the pin count is unchanged",
)
def test_direct_dependency_with_header_and_row_removed_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reqs = replaced(REQS, "# direct: Beta_Tool (dev)\n", "")
    ledger = replaced(LEDGER, "| beta-tool | 3.0 | MIT | dev | why | alt |\n", "")
    rc, _, _ = run(plant(tmp_path, reqs=reqs, ledger=ledger), capsys)
    assert rc == 1


@pytest.mark.parametrize("line", ["  delta==1.0", "\tdelta==1.0 --hash=sha256:4444", "    --hash=sha256:4444"])
def test_indented_pin_line_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str], line: str) -> None:
    rc, out, err = run(plant(tmp_path, reqs=REQS + line + "\n"), capsys)
    assert (rc, out) == (1, "")
    assert "requirements-dev.txt:11: malformed pin line" in err


def test_indented_line_after_a_continuation_must_be_a_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _, err = run(plant(tmp_path, reqs=replaced(REQS, "    --hash=sha256:bbbb", "    delta==1.0 \\")), capsys)
    assert rc == 1
    assert "requirements-dev.txt:8: malformed pin line" in err


def test_duplicate_platform_arm_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = f"\tlinux_x64) echo {'0' * 64} ;;\n"
    scan = replaced(SCAN, f"\tlinux_x64) echo {SHA_A} ;;\n", first + f"\tlinux_x64) echo {SHA_A} ;;\n")
    rc, _, err = run(plant(tmp_path, scan=scan), capsys)
    assert rc == 1
    assert "gitleaks linux_x64: duplicate platform (first at line" in err
    assert "gitleaks linux_x64: digest differs" in err


ZERO = "0" * 64
UNEXPECTED = "unexpected case arm in pinned_sha256 (only '<platform>) echo <sha256> ;;' lines are checked)"


@pytest.mark.parametrize(
    "arm",
    [
        f"\t*) echo {ZERO} ;;",
        f"\tlinux_x64|zz) echo {ZERO} ;;",
        f'\t"linux_x64") echo {ZERO} ;;',
        f"\tlinux_x6?) echo {ZERO} ;;",
    ],
)
def test_earlier_non_standard_digest_arm_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str], arm: str) -> None:
    scan = replaced(SCAN, '\tcase "$1" in\n', f'\tcase "$1" in\n{arm}\n')
    rc, _, err = run(plant(tmp_path, scan=scan), capsys)
    assert rc == 1
    assert f"tools/secret-scan.sh:6: {UNEXPECTED}" in err


@pytest.mark.parametrize(
    ("old", "new", "line"),
    [
        ("\t*) return 1 ;;\n", f"\t*) echo {ZERO} ;;\n", 8),
        (f"\tlinux_x64) echo {SHA_A} ;;\n", f"\t*) return 1 ;;\n\tlinux_x64) echo {SHA_A} ;;\n", 6),
        (f"\tlinux_x64) echo {SHA_A} ;;\n", f"\tlinux_x64)\n\t\techo {SHA_A} ;;\n", 6),
    ],
)
def test_arm_other_than_digests_and_a_final_return_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str, line: int
) -> None:
    rc, _, err = run(plant(tmp_path, scan=replaced(SCAN, old, new)), capsys)
    assert rc == 1
    assert f"tools/secret-scan.sh:{line}: {UNEXPECTED}" in err


@pytest.mark.parametrize(
    "hidden",
    ["\u00a0", "\r", "\x0c", "\x1c", "\x0b"],
    ids=["no-break-space", "lone-carriage-return", "form-feed", "file-separator", "vertical-tab"],
)
def test_arm_behind_a_character_sh_reads_as_text_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hidden: str
) -> None:
    """To Python these look like a comment line or a line break; to sh the line is the arm `<char>#|linux_x64)`."""
    arm = f"\t{hidden}#|linux_x64) echo {ZERO} ;;"
    scan = replaced(SCAN, '\tcase "$1" in\n', f'\tcase "$1" in\n{arm}\n')
    rc, out, err = run(plant(tmp_path, scan=scan), capsys)
    assert (rc, out) == (1, "")
    assert f"tools/secret-scan.sh:6: {UNEXPECTED}" in err


def test_digest_arm_indented_by_a_no_break_space_is_not_a_pin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = replaced(SCAN, f"\tlinux_x64) echo {SHA_A} ;;\n", f"\t\u00a0linux_x64) echo {SHA_A} ;;\n")
    rc, _, err = run(plant(tmp_path, scan=scan), capsys)
    assert rc == 1
    assert f"tools/secret-scan.sh:6: {UNEXPECTED}" in err
    assert "gitleaks linux_x64: platform not pinned" in err


def test_comments_and_blank_lines_in_the_digest_case_are_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scan = replaced(SCAN, "\t*) return 1 ;;\n", "\n\t# unknown platform\n\t*) return 1 ;;\n")
    rc, _, err = run(plant(tmp_path, scan=scan), capsys)
    assert (rc, err) == (0, "")
