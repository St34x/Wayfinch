# SPDX-License-Identifier: Apache-2.0
# Unit tests of the Hermes `glasses` plugin (no Hermes import; runs in the dev venv).
# Standalone: make -f mk/plugin.mk PY=.local/venv-dev/bin/python test-plugin

PY ?= .local/venv-dev/bin/python

.PHONY: test-plugin
TEST_TARGETS += test-plugin

#: test-plugin — unit tests of the Hermes glasses plugin (no Hermes import)
test-plugin:
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/test-plugin hermes-plugin/tests -q
