# SPDX-License-Identifier: Apache-2.0
# Dependency ledger gate (T1.136, ENGINEERING.md sections 8 and 10): tools/quality/check_ledger.py checks that
# docs/dependencies.md has one row per `# direct:` line of requirements-dev.txt at its pinned version and scope, that
# the gitleaks row and digests equal tools/secret-scan.sh, and that every licence exception names a pin. Its tests
# (tools/quality/tests/test_check_ledger*.py; none found = failure) run first.
# Standalone: make -f mk/ledger.mk PY=.local/venv-dev/bin/python check-ledger
PY ?= .local/venv-dev/bin/python

.PHONY: check-ledger
TEST_TARGETS += check-ledger

#: check-ledger — docs/dependencies.md lists exactly the direct requirements at their pins; gitleaks = secret-scan.sh
check-ledger:
	@set -- tools/quality/tests/test_check_ledger*.py; [ -f "$$1" ] || { echo "check-ledger: no tools/quality/tests/test_check_ledger*.py" >&2; exit 2; }; mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/check-ledger "$$@" -q -p no:cacheprovider
	$(PY) tools/quality/check_ledger.py
