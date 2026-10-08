# SPDX-License-Identifier: Apache-2.0
# Self-tests of the repo skeleton (secret scan wrapper and fragment wiring), run by `make test`.
# tools/tests/test_make_fragments.sh is deliberately NOT listed: it runs the real `make test` itself,
# so wiring it in here would recurse. Run it by hand: sh tools/tests/test_make_fragments.sh
# The wiring test gets a clean make environment (no MAKEFLAGS/MAKELEVEL, no exported command-line
# variables) so that `make test PY=...` or -j on the outer make do not leak into the throwaway Makefile
# copies it builds and checks.

.PHONY: test-secret-scan test-make-wiring
TEST_TARGETS += test-secret-scan test-make-wiring

#: test-secret-scan — self-tests of the secret-scan wrapper
test-secret-scan:
	sh tools/secret-scan.sh --version >/dev/null
	sh tools/tests/test_secret_scan.sh
	sh tools/tests/test_secret_scan_edges.sh

#: test-make-wiring — edge cases of the `mk/*.mk` fragment wiring on throwaway Makefile copies
test-make-wiring:
	env -u MAKEFLAGS -u MFLAGS -u MAKELEVEL -u PY -u PYTHON -u VENV -u TEST_TARGETS \
		sh tools/tests/test_make_fragments_edges.sh
