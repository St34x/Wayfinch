# SPDX-License-Identifier: Apache-2.0
# Docs gate (T1.134, ENGINEERING.md section 9): tools/quality/check_docs.py checks every tracked *.md - relative
# links and #anchors resolve (fenced code, code spans and HTML comments ignored, external URLs not fetched), every
# public document (all but the private folder of check_hygiene.py) is reachable by links from README.md
# (docs/README.md is the index) and links nothing in the private folder, the private knowledge-base INDEX.md exists
# while that folder is tracked, and every INDEX.md lists exactly the documents of its directory. Markdown parsing:
# tools/quality/md_links.py. Every tools/quality/tests/test_check_docs*.py runs first; none found fails the target.
# Standalone: make -f mk/docs.mk PY=.local/venv-dev/bin/python check-docs
PY ?= .local/venv-dev/bin/python

.PHONY: check-docs
TEST_TARGETS += check-docs

#: check-docs — internal links and anchors of every tracked *.md resolve, every public document is indexed
check-docs:
	@set -- tools/quality/tests/test_check_docs*.py; [ -f "$$1" ] || { echo "check-docs: no tools/quality/tests/test_check_docs*.py" >&2; exit 2; }; mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/check-docs "$$@" -q -p no:cacheprovider
	$(PY) tools/quality/check_docs.py
