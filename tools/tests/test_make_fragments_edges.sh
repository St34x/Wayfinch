#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Edge cases for the mk/*.mk fragment wiring (complements test_make_fragments.sh).
# Runs a copy of the Makefile in a temp dir with a stub secret scan. Cases print "ok", "FAIL" or
# "note" (a documented gap that does not fail the run).
#
# Usage: sh tools/tests/test_make_fragments_edges.sh   (from the repo root; MAKE=... to override make)
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
MAKE=${MAKE:-make}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

PASS=0
FAIL=0
NOTE=0
ok() { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; [ -f "$WORK/out" ] && sed 's/^/       | /' "$WORK/out"; return 0; }
note() { NOTE=$((NOTE + 1)); echo "note - $1"; }

S="$WORK/tree"
fresh() { # fresh tree: Makefile copy, stub secret scan, empty mk/
	rm -rf "$S"
	mkdir -p "$S/tools" "$S/mk"
	cp "$ROOT/Makefile" "$S/Makefile"
	printf '#!/bin/sh\necho "stub secret-scan ran"\ntouch scan.ran\n' >"$S/tools/secret-scan.sh"
}
run_make() {
	set +e
	$MAKE --no-print-directory -C "$S" "$@" >"$WORK/out" 2>&1
	RC=$?
	set -e
}
frag() { # frag <file> <target> [recipe]
	printf '# SPDX-License-Identifier: Apache-2.0\n.PHONY: %s\nTEST_TARGETS += %s\n%s:\n\t%s\n' \
		"$2" "$2" "$2" "${3:-@touch $2.ran}" >"$S/mk/$1"
}

# F1. Fragment with a syntax error: make test fails loudly (never silently skipped).
fresh
printf '# SPDX-License-Identifier: Apache-2.0\nTEST_TARGETS += test-x\ntest-x\n\t@echo x\n' >"$S/mk/bad.mk"
run_make test
[ "$RC" -ne 0 ] && grep -q 'missing separator' "$WORK/out" && [ ! -f "$S/scan.ran" ] &&
	ok "fragment with a syntax error fails make test" || bad "syntax error fragment (rc=$RC)"

# F2. Fragment that references an undefined target in TEST_TARGETS: make test fails.
fresh
printf '# SPDX-License-Identifier: Apache-2.0\nTEST_TARGETS += test-missing\n' >"$S/mk/ghost.mk"
run_make test
[ "$RC" -ne 0 ] && grep -q "No rule to make target 'test-missing'" "$WORK/out" &&
	ok "TEST_TARGETS entry without a rule fails make test" || bad "ghost target (rc=$RC)"

# F3. Two fragments define the same target: make warns; only the last recipe runs.
fresh
frag a.mk test-dup '@touch A.ran'
frag b.mk test-dup '@touch B.ran'
run_make test
if [ "$RC" -eq 0 ] && grep -q "overriding recipe for target 'test-dup'" "$WORK/out"; then
	ok "duplicate target is at least reported as a make warning"
else
	bad "duplicate target warning (rc=$RC)"
fi
[ -f "$S/A.ran" ] || note "duplicate target: first fragment's recipe silently dropped, make test still green"

# F4. A fragment cannot replace the real secret scan or the test recipe (the Makefile defines them last).
fresh
printf '# SPDX-License-Identifier: Apache-2.0\nsecret-scan:\n\t@echo fake-scan\ntest:\n\t@echo fake-test\n' \
	>"$S/mk/evil.mk"
run_make test
[ "$RC" -eq 0 ] && [ -f "$S/scan.ran" ] && ! grep -q 'fake-scan' "$WORK/out" && ! grep -q 'fake-test' "$WORK/out" &&
	ok "fragment cannot override secret-scan/test recipes" || bad "fragment overrides builtin (rc=$RC)"

# F5. A failing fragment and -j: make test still fails (no masking under parallel make).
fresh
frag a.mk test-a
frag z.mk test-z '@false'
run_make -j4 test
[ "$RC" -ne 0 ] && ok "failing fragment fails make -j4 test" || bad "failing fragment under -j4 (rc=$RC)"

# F6. PY passed on the command line reaches fragments; default is the venv path.
fresh
frag py.mk test-py '@echo "PY=$(PY)"'
run_make test
grep -q 'PY=.local/venv-dev/bin/python' "$WORK/out" && ok "fragments see default PY" || bad "default PY"
run_make test PY=/opt/other/python
grep -q 'PY=/opt/other/python' "$WORK/out" && ok "PY=... override reaches fragments" || bad "PY override"

# F7. Non-.mk files and sub-directories in mk/ are not included.
fresh
printf 'this is not make syntax\n' >"$S/mk/README.md"
mkdir -p "$S/mk/sub"
frag sub/nested.mk test-nested
run_make test
[ "$RC" -eq 0 ] && [ ! -f "$S/test-nested.ran" ] && ok "mk/README.md and mk/sub/*.mk are not included" ||
	bad "non-fragment files included (rc=$RC)"

# F8. Fragment file name with a space: $(wildcard) splits it, -include drops both halves silently.
fresh
frag 'my frag.mk' test-space
run_make test
if [ "$RC" -eq 0 ] && [ ! -f "$S/test-space.ran" ]; then
	note "fragment 'mk/my frag.mk' is silently ignored (rc=0, its test never runs)"
else
	ok "fragment with a space in its name runs or fails loudly"
fi

# F9. A fragment that assigns TEST_TARGETS with := drops earlier fragments' targets silently.
fresh
frag a.mk test-a
printf '# SPDX-License-Identifier: Apache-2.0\nTEST_TARGETS := test-z\ntest-z:\n\t@touch test-z.ran\n' >"$S/mk/z.mk"
run_make test
if [ "$RC" -eq 0 ] && [ ! -f "$S/test-a.ran" ]; then
	note "'TEST_TARGETS :=' in a later fragment silently drops earlier targets (mk/README.md forbids it; not enforced)"
else
	ok "earlier fragment targets survive ':='"
fi

echo "test_make_fragments_edges: $PASS passed, $FAIL failed, $NOTE notes"
[ "$FAIL" -eq 0 ]
