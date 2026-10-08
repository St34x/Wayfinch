# SPDX-License-Identifier: Apache-2.0
# Size gate - code tree checks: tools/quality/check_size.py (file 400 / function 60 lines; targets warn).
# Standalone: make -f mk/quality.mk PY=.local/venv-dev/bin/python check-size

PY ?= .local/venv-dev/bin/python

.PHONY: check-size
TEST_TARGETS += check-size

#: check-size — file and function length limits (tools/quality/check_size.py with its baseline)
check-size:
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/check-size tools/quality/tests/test_check_size*.py -q -p no:cacheprovider
	$(PY) tools/quality/check_size.py
