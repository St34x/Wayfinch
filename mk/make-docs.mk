# SPDX-License-Identifier: Apache-2.0
# Make-target docs gate (T1.135, ENGINEERING.md sections 9 and 10): tools/codegen/gen_make_docs.py --check fails
# when a target of the Makefile or a mk/*.mk fragment has no `#: <target> — <description>` line directly above its
# rule, when docs/reference/make-targets.md (generated from those lines; that one file, not the directory) is stale,
# when a target `make test` runs is in no live row of the ENGINEERING.md section 10 gate table, when a live row
# names an undefined target, or when a tracked public *.md runs `make <t>` (code span or fenced block) for a target
# that is neither defined nor in a planned row. Runs its own tests first (none found = fail).
# After adding or changing a target or its `#:` line: python3 tools/codegen/gen_make_docs.py and
# commit the reference.
# Standalone: make -f mk/make-docs.mk PY=.local/venv-dev/bin/python check-make-docs
PY ?= .local/venv-dev/bin/python

.PHONY: check-make-docs
TEST_TARGETS += check-make-docs

#: check-make-docs — make-target reference fresh, every target described, gate table and docs name real targets
check-make-docs:
	@set -- tools/codegen/tests/test_gen_make_docs*.py; [ -f "$$1" ] || { echo "check-make-docs: no tools/codegen/tests/test_gen_make_docs*.py" >&2; exit 2; }; \
	mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/check-make-docs "$$@" -q -p no:cacheprovider
	$(PY) tools/codegen/gen_make_docs.py --check
