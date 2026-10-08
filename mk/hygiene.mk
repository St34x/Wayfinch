# SPDX-License-Identifier: Apache-2.0
# Repo hygiene gate (T1.102, ENGINEERING.md sections 1, 3, 4, 9): marker keywords in comments must carry a task id,
# docs/glossary.md defines every term once, no file cites the private team notes (folder, decision records,
# reports; T1.141) or carries a review-process tag (QA wave/round ids, review/QA/fix round numbers, wave numbers,
# team-lead lists or decisions, `_qa` file-name suffixes; exact rule: ENGINEERING.md section 9), and every module
# README passes tools/quality/check_readmes.py with the module list
# tools/quality/modules.txt (T1.103). Commit subjects are not checked here but at merge time, from the
# recorded gate start commit (tools/quality/commit-gate-start.txt, filled by the merge step) to HEAD:
#   python3 tools/quality/check_hygiene.py --commits
# Standalone: make -f mk/hygiene.mk PY=.local/venv-dev/bin/python check-hygiene
PY ?= .local/venv-dev/bin/python

.PHONY: check-hygiene
TEST_TARGETS += check-hygiene

#: check-hygiene — task ids on marker keywords, unique glossary terms, no private references or process tags, module READMEs
check-hygiene:
	@set -- tools/quality/tests/test_check_hygiene*.py; [ -f "$$1" ] || { echo "check-hygiene: no tools/quality/tests/test_check_hygiene*.py" >&2; exit 2; }; mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/check-hygiene "$$@" -q -p no:cacheprovider
	$(PY) tools/quality/check_hygiene.py
	@set -- tools/quality/tests/test_check_readmes*.py; [ -f "$$1" ] || { echo "check-hygiene: no tools/quality/tests/test_check_readmes*.py" >&2; exit 2; }; mkdir -p .local/pytest-tmp && $(PY) -m pytest --basetemp=.local/pytest-tmp/check-hygiene-readmes "$$@" -q -p no:cacheprovider && $(PY) tools/quality/check_readmes.py
