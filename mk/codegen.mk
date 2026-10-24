# SPDX-License-Identifier: Apache-2.0
# Codegen freshness gate (T1.104; ENGINEERING.md section 4): the whole generated directory is regenerated into a
# temporary directory and compared with `diff -r` (tools/codegen/check_fresh.sh); an extra, missing or edited file
# fails. T1.105: the Kotlin Envelope.kt is rendered again and compared (gen_kotlin.py --check; a missing, edited,
# stale or symlinked file, or any other file in its directory, fails). T1.106: docs/reference/config.md (that one
# file, not the directory) is regenerated from the plugin config schema and compared (gen_config_docs.py --check; a
# missing, edited or symlinked file fails). Then every tools/codegen/tests/test_*.py except test_gen_make_docs*.py
# (those run in check-make-docs) runs (none found = fail).
# After a protocol schema edit: sh tools/codegen/regenerate.sh .local/venv-dev/bin/python (Python types and Envelope.kt)
# or, for the config schema, .local/venv-dev/bin/python tools/codegen/gen_config_docs.py; commit the output. The
# generators are stdlib-only, so any Python >= 3.11 runs them; the gate itself uses $(PY).
# Standalone: make -f mk/codegen.mk PY=.local/venv-dev/bin/python check-codegen
PY ?= .local/venv-dev/bin/python

.PHONY: check-codegen
TEST_TARGETS += check-codegen

#: check-codegen — generated protocol types, the shipped schema copy and docs/reference/config.md match their schemas; the codegen tests
check-codegen:
	sh tools/codegen/check_fresh.sh $(PY)
	$(PY) tools/codegen/gen_config_docs.py --check
	$(PY) tools/codegen/gen_kotlin.py --check
	@set -- $(filter-out tools/codegen/tests/test_gen_make_docs%,$(wildcard tools/codegen/tests/test_*.py)); \
	[ -f "$${1:-}" ] || { echo "check-codegen: no tests in tools/codegen/tests" >&2; exit 2; }; \
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/codegen "$$@" -q -p no:cacheprovider
	@echo "check-codegen: OK (protocol types and docs/reference/config.md match their schemas)"
