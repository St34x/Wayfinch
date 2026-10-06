# SPDX-License-Identifier: Apache-2.0
# Import-boundary gate (T1.100, ENGINEERING.md section 1): the pure core imports no adapter, framework, Hermes
# package or I/O. check_core_purity.py checks every direct import of a core module against an allowlist and bans
# builtin I/O, clock reads and console streams; lint-imports checks the forbidden contract in .importlinter
# (transitive paths) from hermes-plugin/ (where the root package lives). Runs every test file of this gate
# (tools/quality/tests/test_imports*.py; none found = failure). lint-imports comes from the venv of $(PY).
# Standalone: make -f mk/imports.mk PY=.local/venv-dev/bin/python check-imports
PY ?= .local/venv-dev/bin/python
IMPORTS_TESTS = $(sort $(wildcard tools/quality/tests/test_imports*.py))

.PHONY: check-imports
TEST_TARGETS += check-imports

check-imports:
	@test -n "$(IMPORTS_TESTS)" || { echo "check-imports: no tools/quality/tests/test_imports*.py found" >&2; exit 1; }
	$(PY) -m pytest $(IMPORTS_TESTS) -q -p no:cacheprovider
	cd hermes-plugin && $(dir $(abspath $(PY)))lint-imports --config ../.importlinter --no-cache --no-logo
	$(PY) tools/quality/check_core_purity.py
