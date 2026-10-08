# SPDX-License-Identifier: Apache-2.0
# Type gate (T1.99, ENGINEERING.md section 5): mypy --strict over hermes-plugin/glasses and tools/, configured
# (scope, Hermes boundary, ratchet baseline) in mypy.ini only; tools/quality/check_mypy_config.py rejects any other
# mypy.ini key or section and inline suppressions in the checked files.
# Standalone: make -f mk/typecheck-py.mk PY=.local/venv-dev/bin/python typecheck-py

PY ?= .local/venv-dev/bin/python
TYPECHECK_PY_TESTS := $(sort $(wildcard tools/quality/tests/test_mypy_*.py))

.PHONY: typecheck-py
TEST_TARGETS += typecheck-py

#: typecheck-py — mypy --strict over hermes-plugin/glasses and tools (mypy.ini only)
typecheck-py:
	@test -n "$(TYPECHECK_PY_TESTS)" || { echo "typecheck-py: no test matches tools/quality/tests/test_mypy_*.py" >&2; exit 1; }
	$(PY) tools/quality/check_mypy_config.py
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/typecheck-py $(TYPECHECK_PY_TESTS) -q -p no:cacheprovider
	$(PY) -m mypy --config-file mypy.ini
