# SPDX-License-Identifier: Apache-2.0
# Coverage gate (T1.101, ENGINEERING.md section 6): the plugin tests under coverage with .coveragerc, then
# tools/quality/coverage_areas.py - core files (the core list of .importlinter) >= 90 %, every other plugin file
# >= 70 % (or its exact ratchet entry), plus one `coverage report --fail-under` run per area. Runs every test file of
# this gate first (tools/quality/tests/test_coverage_*.py; none found = failure). Data: .local/coverage-py/, deleted
# before every run, so the checker only ever judges data of this run: a run that collects nothing (e.g. coverage
# switched off by a pytest.ini `addopts = --no-cov`) leaves no data file and the checker exits 2.
# Standalone: make -f mk/coverage-py.mk PY=.local/venv-dev/bin/python coverage-py
PY ?= .local/venv-dev/bin/python
COVERAGE_PY_TESTS := $(sort $(wildcard tools/quality/tests/test_coverage_*.py))

.PHONY: coverage-py
TEST_TARGETS += coverage-py

#: coverage-py — plugin test coverage: core files >= 90 %, other plugin files >= 70 % or their ratchet entry
coverage-py:
	@test -n "$(COVERAGE_PY_TESTS)" || { echo "coverage-py: no tools/quality/tests/test_coverage_*.py found" >&2; exit 1; }
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/coverage-py $(COVERAGE_PY_TESTS) -q -p no:cacheprovider
	rm -rf .local/coverage-py
	$(PY) -m pytest --basetemp=.local/pytest-tmp/coverage-py-plugin hermes-plugin/tests -q -p no:cacheprovider --cov --cov-config=.coveragerc --cov-report=
	$(PY) tools/quality/coverage_areas.py
