#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Tests the Makefile fragment wiring: `make test` = secret-scan + every target a mk/*.mk fragment appends
# to TEST_TARGETS. Cases 1-5 run against a copy of the Makefile in a temp dir with a stub secret scan
# (fast, independent of other fragments); case 6 plants mk/zz-demo.mk in the real tree, checks that
# `gen_make_docs.py --check` flags it (no live ENGINEERING.md section 10 row, stale reference), then runs the
# real `make test -o check-make-docs` (that gate skipped; the fragment is removed afterwards, also on failure).
#
# Usage: sh tools/tests/test_make_fragments.sh   (from the repo root)
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
MAKE=${MAKE:-make}
WORK=$(mktemp -d)
DEMO="$ROOT/mk/zz-demo.mk"
PLANTED=0
cleanup() {
	[ "$PLANTED" -eq 1 ] && rm -f "$DEMO"
	rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; [ -f "$WORK/out" ] && sed 's/^/       | /' "$WORK/out"; return 0; }
run_make() { # run_make <dir> <args...>; sets RC, output in $WORK/out
	d=$1
	shift
	set +e
	$MAKE --no-print-directory -C "$d" "$@" >"$WORK/out" 2>&1
	RC=$?
	set -e
}

# A fragment that records that it ran ($1 = name, $2 = marker file).
write_fragment() {
	cat <<EOF
# SPDX-License-Identifier: Apache-2.0
.PHONY: test-$1
TEST_TARGETS += test-$1
#: test-$1 — demo fragment $1
test-$1:
	@echo "fragment $1 ran"
	@touch $2
EOF
}

S="$WORK/tree"
mkdir -p "$S/tools"
cp "$ROOT/Makefile" "$S/Makefile"
printf '#!/bin/sh\necho "stub secret-scan ran"\n' >"$S/tools/secret-scan.sh"

# 1. No fragments (no mk/ dir at all): make test is green and runs the secret scan.
run_make "$S" test
[ "$RC" -eq 0 ] && grep -q 'stub secret-scan ran' "$WORK/out" && ok "make test green with no fragments" ||
	bad "make test green with no fragments (rc=$RC)"

# 2. A planted fragment runs inside make test.
mkdir -p "$S/mk"
write_fragment demo "$WORK/demo.ran" >"$S/mk/zz-demo.mk"
run_make "$S" test
[ "$RC" -eq 0 ] && [ -f "$WORK/demo.ran" ] && grep -q 'stub secret-scan ran' "$WORK/out" &&
	ok "planted fragment runs inside make test" || bad "planted fragment runs inside make test (rc=$RC)"

# 3. Several fragments all run.
write_fragment alpha "$WORK/alpha.ran" >"$S/mk/aa-alpha.mk"
rm -f "$WORK/demo.ran"
run_make "$S" test
[ "$RC" -eq 0 ] && [ -f "$WORK/alpha.ran" ] && [ -f "$WORK/demo.ran" ] && ok "all fragments run" ||
	bad "all fragments run (rc=$RC)"

# 4. A failing fragment target fails make test.
printf '# SPDX-License-Identifier: Apache-2.0\n.PHONY: test-broken\nTEST_TARGETS += test-broken\ntest-broken:\n\t@false\n' \
	>"$S/mk/zz-broken.mk"
run_make "$S" test
[ "$RC" -ne 0 ] && ok "failing fragment fails make test" || bad "failing fragment fails make test (rc=$RC)"
rm -f "$S/mk/zz-broken.mk"

# 5. A fragment never becomes the default goal (plain `make` shows help, runs no tests).
rm -f "$WORK/alpha.ran" "$WORK/demo.ran"
run_make "$S"
[ "$RC" -eq 0 ] && [ ! -f "$WORK/alpha.ran" ] && [ ! -f "$WORK/demo.ran" ] && grep -q 'make test' "$WORK/out" &&
	ok "default goal is help, not a fragment" || bad "default goal is help, not a fragment (rc=$RC)"

# 5b. The fragment is runnable alone (before the owner Makefile is merged).
run_make "$S" -f mk/zz-demo.mk test-demo
[ "$RC" -eq 0 ] && [ -f "$WORK/demo.ran" ] && ok "fragment runs standalone with make -f" ||
	bad "fragment runs standalone with make -f (rc=$RC)"

# 6. Real tree: planted mk/zz-demo.mk runs inside the real `make test` (with the real secret scan).
if [ -e "$DEMO" ]; then
	bad "real tree: $DEMO already exists, refusing to overwrite it"
else
	PLANTED=1
	write_fragment zz-demo "$WORK/real.ran" >"$DEMO"
	set +e
	(cd "$ROOT" && python3 tools/codegen/gen_make_docs.py --check) >"$WORK/out" 2>&1
	RC=$?
	set -e
	[ "$RC" -eq 1 ] && grep -q 'mk/zz-demo.mk:3: `make test` runs `test-zz-demo`, but no live row' "$WORK/out" &&
		ok "real tree: check-make-docs flags the planted fragment" ||
		bad "real tree: check-make-docs flags the planted fragment (rc=$RC)"
	run_make "$ROOT" test -o check-make-docs
	[ "$RC" -eq 0 ] && [ -f "$WORK/real.ran" ] && ok "real tree: planted mk/zz-demo.mk runs inside make test" ||
		bad "real tree: planted mk/zz-demo.mk runs inside make test (rc=$RC)"
	rm -f "$DEMO"
	PLANTED=0
	[ ! -e "$DEMO" ] && ok "real tree: planted fragment removed" || bad "real tree: planted fragment removed"
fi

echo "test_make_fragments: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
