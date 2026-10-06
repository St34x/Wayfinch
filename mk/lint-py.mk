# SPDX-License-Identifier: Apache-2.0
# Python lint + format gate (T1.97, ENGINEERING.md sections 2, 3, 5): ruff with ruff.toml (rules, limits, ratchet
# baseline) over hermes-plugin, protocol and tools; the baseline test fails on a stale entry or one without a task id.
# --config ruff.toml is explicit, so a nested ruff config cannot replace the root one for its subtree. Every
# tools/quality/tests/test_ruff_*.py runs (baseline, bypass, exemption forms, Python floor and scope); none found
# fails the target.
# Standalone: make -f mk/lint-py.mk PY=.local/venv-dev/bin/python lint-py
PY ?= .local/venv-dev/bin/python
LINT_PY_DIRS := hermes-plugin protocol tools
LINT_PY_TESTS := $(sort $(wildcard tools/quality/tests/test_ruff_*.py))

.PHONY: lint-py
TEST_TARGETS += lint-py

lint-py:
	$(PY) -m ruff check --no-cache --config ruff.toml $(LINT_PY_DIRS)
	$(PY) -m ruff format --no-cache --config ruff.toml --check $(LINT_PY_DIRS)
	@test -n "$(LINT_PY_TESTS)" || { echo "lint-py: no tools/quality/tests/test_ruff_*.py found" >&2; exit 1; }
	$(PY) -m pytest $(LINT_PY_TESTS) -q -p no:cacheprovider
