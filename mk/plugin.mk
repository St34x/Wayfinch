# SPDX-License-Identifier: Apache-2.0
# Unit tests of the Hermes `glasses` plugin (no Hermes import; runs in the dev venv).
# Standalone: make -f mk/plugin.mk PY=.local/venv-dev/bin/python test-plugin

PY ?= .local/venv-dev/bin/python

.PHONY: test-plugin
TEST_TARGETS += test-plugin

test-plugin:
	$(PY) -m pytest hermes-plugin/tests -q
