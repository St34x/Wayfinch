# SPDX-License-Identifier: Apache-2.0
# Protocol schema tests (T1.02). Included by the top-level Makefile via mk/*.mk;
# standalone: make -f mk/protocol.mk PY=.local/venv-dev/bin/python test-protocol
PY ?= .local/venv-dev/bin/python

.PHONY: test-protocol
TEST_TARGETS += test-protocol

#: test-protocol — protocol schema and fixture tests
test-protocol:
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/test-protocol protocol/tests -q -p no:cacheprovider
