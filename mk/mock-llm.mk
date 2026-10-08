# SPDX-License-Identifier: Apache-2.0
# Mock LLM unit tests (T1.112): the script directives (tools/mock-llm/mock_script.py) and the HTTP glue
# (tools/mock-llm/mock_llm.py, served on a free 127.0.0.1 port inside the test; no network, no test Hermes).
# The mock LLM itself is started by test-hermes-up (mk/test-hermes.mk).
# Standalone: make -f mk/mock-llm.mk PY=.local/venv-dev/bin/python test-mock-llm
PY ?= .local/venv-dev/bin/python

.PHONY: test-mock-llm
TEST_TARGETS += test-mock-llm

#: test-mock-llm — unit tests of the scripted mock LLM (directives and HTTP glue; no network)
test-mock-llm:
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/test-mock-llm tools/mock-llm/tests -q -p no:cacheprovider
