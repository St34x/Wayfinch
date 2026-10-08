# SPDX-License-Identifier: Apache-2.0
# Developer entry point.
#   make test          secret scan + every test target that a fragment in mk/*.mk appends to TEST_TARGETS
#   make secret-scan   pinned gitleaks over the working tree and the history of HEAD (tools/secret-scan.sh)
#   make venv          (re)build the Python test venv $(VENV) from requirements-dev.txt
# Fragments: see mk/README.md. Only this file defines the wiring; components add mk/<name>.mk.

PY ?= .local/venv-dev/bin/python
PYTHON ?= python3
VENV ?= .local/venv-dev
TEST_TARGETS :=

-include $(sort $(wildcard mk/*.mk))

.DEFAULT_GOAL := help
.PHONY: help test secret-scan venv

#: help — list the top-level targets and the `TEST_TARGETS` that `make test` runs
help:
	@echo "make test         secret scan + fragment test targets:$(if $(strip $(TEST_TARGETS)), $(strip $(TEST_TARGETS)), (none))"
	@echo "make secret-scan  gitleaks (pinned + checksum-verified) over working tree and history"
	@echo "make venv         build $(VENV) from requirements-dev.txt with $(PYTHON)"

#: test — the secret scan plus every target a fragment appends to `TEST_TARGETS`
test: secret-scan $(TEST_TARGETS)
	@echo "make test: OK (secret-scan$(if $(strip $(TEST_TARGETS)), $(strip $(TEST_TARGETS))))"

#: secret-scan — pinned, checksum-verified gitleaks over the working tree and the history of HEAD
secret-scan:
	sh tools/secret-scan.sh

#: venv — (re)build the Python test venv (VENV, default .local/venv-dev) from the hash-pinned requirements-dev.txt
venv:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --disable-pip-version-check --require-hashes --only-binary :all: \
		-r requirements-dev.txt
