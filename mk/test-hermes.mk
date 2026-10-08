# SPDX-License-Identifier: Apache-2.0
# Isolated test Hermes (pinned clone + scratch home under $(TEST_HERMES_LOCAL)); never touches a live install.
#   make test-hermes-harness  hermetic tests of tools/test-hermes (fake hermes, temp dirs, no ports, no network,
#                             only $(PY) with PyYAML); part of make test
#   make test-hermes-up       test home + mock LLM (127.0.0.1:$(TEST_LLM_PORT), background) + gateway (foreground,
#                             glasses plugin on 127.0.0.1:$(TEST_GATEWAY_PORT)); Ctrl-C stops the gateway
#   make test-hermes-down     stop the gateway and the mock LLM via their pid files, only while the pid still
#                             runs that program (tools/test-hermes/stop_pidfile.sh); pid files are always removed
# test-hermes-up/down are NOT in TEST_TARGETS: they need the pinned venv and run a real gateway.
# Getting the pinned clone and its venv (Python 3.14): docs/test-hermes.md.
# The mock LLM runs as /usr/bin/python3 in a clean env (stdlib only, any Python >= 3.11). That path is fixed in the
# recipe below, not a variable; on a host without /usr/bin/python3 start the mock LLM by hand (docs/test-hermes.md).
# Variables (environment or command line): TEST_HERMES_LOCAL, TEST_HERMES_VENV, HERMES_REF, TEST_LLM_PORT,
# TEST_GATEWAY_PORT, TEST_PROFILES -- see tools/test-hermes/setup_home.sh.

PY ?= .local/venv-dev/bin/python
TEST_HERMES_LOCAL ?= $(CURDIR)/.local
TEST_LLM_PORT ?= 18080
TEST_GATEWAY_PORT ?= 18700
export TEST_HERMES_LOCAL TEST_LLM_PORT TEST_GATEWAY_PORT

.PHONY: test-hermes-harness test-hermes-up test-hermes-down
TEST_TARGETS += test-hermes-harness

#: test-hermes-harness — hermetic tests of the test-Hermes harness (fake hermes, temp dirs, no ports, no network)
test-hermes-harness:
	PY=$(PY) sh tools/test-hermes/test_harness.sh

#: test-hermes-up — start the isolated test Hermes: test home, mock LLM in the background, gateway in the foreground
test-hermes-up:
	mkdir -p $(TEST_HERMES_LOCAL)
	sh tools/test-hermes/setup_home.sh
	env -i PATH=/usr/bin:/bin /usr/bin/python3 tools/mock-llm/mock_llm.py --port $(TEST_LLM_PORT) --log-file $(TEST_HERMES_LOCAL)/mock-llm.jsonl & echo $$! > $(TEST_HERMES_LOCAL)/mock-llm.pid
	sh tools/test-hermes/run_gateway.sh

#: test-hermes-down — stop the test gateway and the mock LLM via their pid files
test-hermes-down:
	sh tools/test-hermes/stop_pidfile.sh $(TEST_HERMES_LOCAL)/gateway.pid 'hermes gateway run'
	sh tools/test-hermes/stop_pidfile.sh $(TEST_HERMES_LOCAL)/mock-llm.pid 'mock_llm.py --port $(TEST_LLM_PORT)'
