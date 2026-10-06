# SPDX-License-Identifier: Apache-2.0
# Offline tamper tests of the `make venv` install (hash pins, no sdist fallback), run by `make test`.
# Standalone: make -f mk/venv.mk test-venv-hashes
# Clean make environment for the Makefile copies the test builds (as test-make-wiring in skeleton.mk).

PYTHON ?= python3

.PHONY: test-venv-hashes
TEST_TARGETS += test-venv-hashes

test-venv-hashes:
	env -u MAKEFLAGS -u MFLAGS -u MAKELEVEL -u PY -u VENV -u TEST_TARGETS PYTHON=$(PYTHON) \
		sh tools/tests/test_venv_hashes.sh
