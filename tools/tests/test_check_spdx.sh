#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Tests for tools/check-spdx.sh and for the LICENSE file.
# Each case builds a throwaway git repo, stages files and runs the checker in it.
# Usage: sh tools/tests/test_check_spdx.sh   (from anywhere inside the repo)
set -u

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CHECK="$ROOT/tools/check-spdx.sh"
# Canonical https://www.apache.org/licenses/LICENSE-2.0.txt (11358 bytes).
LICENSE_SHA256=cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30

WORK=$(mktemp -d "${TMPDIR:-/tmp}/test-check-spdx.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
ko() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }

# new_repo NAME -> creates $WORK/NAME as an empty git repo and cd's into it
new_repo() {
	R="$WORK/$1"
	mkdir -p "$R" && cd "$R" || exit 1
	git -c init.defaultBranch=main init -q .
}

# put PATH CONTENT -> writes a file (creating dirs) and stages it
put() {
	mkdir -p "$(dirname "$1")"
	printf '%s\n' "$2" >"$1"
	git add -- "$1"
}

# run_check -> runs the checker in the current repo; sets $rc and $out
run_check() {
	out=$(sh "$CHECK" 2>&1)
	rc=$?
}

# expect RC NAME [PATTERN-in-output]
expect() {
	if [ "$rc" -ne "$1" ]; then
		ko "$2 (exit $rc, want $1): $out"
		return
	fi
	if [ $# -ge 3 ] && ! printf '%s\n' "$out" | grep -F -q -- "$3"; then
		ko "$2 (output lacks '$3'): $out"
		return
	fi
	ok "$2"
}

H_HASH='# SPDX-License-Identifier: Apache-2.0'
H_SLASH='// SPDX-License-Identifier: Apache-2.0'

# A repo where every covered kind of file has a correct header.
good_tree() {
	put app.py "$(printf '#!/usr/bin/env python3\n%s\nprint(1)' "$H_HASH")"
	put run.sh "$(printf '#!/bin/sh\n%s\necho hi' "$H_HASH")"
	put Makefile "$(printf '%s\nall:\n\ttrue' "$H_HASH")"
	put mk/x.mk "$(printf '%s\n.PHONY: x' "$H_HASH")"
	put android/app/src/Main.kt "$(printf '%s\npackage a' "$H_SLASH")"
	put android/app/build.gradle.kts "$(printf '%s\nplugins {}' "$H_SLASH")"
	put android/settings.gradle "$(printf '%s\ninclude(":app")' "$H_SLASH")"
	put android/Block.kt "$(printf '/*\n * SPDX-License-Identifier: Apache-2.0\n */\npackage b')"
}

# 1. clean tree passes
new_repo clean
good_tree
run_check
expect 0 "all covered types with header pass" "OK"

# 2. the planted header-less file fails and is named (the acceptance test)
new_repo planted
good_tree
put hermes-plugin/planted.py 'print("no header")'
run_check
expect 1 "planted header-less .py fails" "hermes-plugin/planted.py"

# 3. every covered type is checked
for f in x.py x.kt x.kts x.sh x.mk Makefile sub/Makefile x.gradle app/build.gradle.kts; do
	new_repo "type-$(printf '%s' "$f" | tr '/.' '__')"
	good_tree
	put "$f" 'no header here'
	run_check
	expect 1 "header-less $f fails" "$f"
done

# 4. data files and Markdown are exempt
new_repo exempt
good_tree
put protocol/envelope.schema.json '{"a": 1}'
put deploy/config.example.yaml 'a: 1'
put deploy/other.yml 'a: 1'
put README.md '# readme'
put android/gradle.properties 'appId=app.example'
put tests/fixtures/sample.txt 'fixture'
put NOTICE 'notice'
run_check
expect 0 "JSON/YAML/Markdown/properties/fixtures exempt" "OK"

# 5. untracked files are not checked
new_repo untracked
good_tree
printf 'print(1)\n' >loose.py
run_check
expect 0 "untracked header-less file ignored" "OK"

# 6. the private team folder is out of scope (never published)
new_repo team
good_tree
put .team/spikes/S99/spike.py 'print("spike")'
run_check
expect 0 ".team/ is exempt" "OK"

# 7. the identifier must be in the header (first 10 lines)
new_repo late
good_tree
put late.py "$(printf 'a=1\na=2\na=3\na=4\na=5\na=6\na=7\na=8\na=9\na=10\n%s' "$H_HASH")"
run_check
expect 1 "identifier on line 11 fails" "late.py"

new_repo line10
good_tree
put line10.py "$(printf 'a=1\na=2\na=3\na=4\na=5\na=6\na=7\na=8\na=9\n%s' "$H_HASH")"
run_check
expect 0 "identifier on line 10 passes" "OK"

# 8. wrong or look-alike licence identifiers fail
new_repo mit
good_tree
put mit.py '# SPDX-License-Identifier: MIT'
run_check
expect 1 "MIT identifier fails" "mit.py"

new_repo lookalike
good_tree
put la.py '# SPDX-License-Identifier: Apache-2.0-or-later'
put lb.py '# SPDX-License-Identifier: Apache-2.01'
put lc.py '# Apache-2.0'
run_check
expect 1 "look-alike identifiers fail" "la.py"
expect 1 "look-alike identifiers fail (2)" "lb.py"
expect 1 "look-alike identifiers fail (3)" "lc.py"

new_repo compound
good_tree
put port.py '# SPDX-License-Identifier: Apache-2.0 AND MIT'
run_check
expect 0 "compound 'Apache-2.0 AND MIT' passes" "OK"

# 9. names with spaces are handled and reported
new_repo spaces
good_tree
put 'dir with space/my script.sh' 'echo no header'
run_check
expect 1 "file name with spaces fails and is named" "dir with space/my script.sh"

new_repo tabname
good_tree
put "$(printf 'tab\there.py')" "$H_HASH"
run_check
expect 1 "file name git must quote is refused" "unusual file name"

# 10. all offenders are listed, not just the first
new_repo many
good_tree
put a.py 'x=1'
put b.sh 'echo'
run_check
if [ "$rc" -eq 1 ] && printf '%s\n' "$out" | grep -q 'a\.py' && printf '%s\n' "$out" | grep -q 'b\.sh'; then
	ok "every offender listed"
else
	ko "every offender listed (exit $rc): $out"
fi

# 11. a tracked file deleted from the work tree does not crash the check
new_repo deleted
good_tree
put gone.py "$H_HASH"
rm gone.py
run_check
expect 0 "staged-but-deleted file skipped" "OK"

# 12. outside a git repo -> usage error (exit 2)
mkdir -p "$WORK/nogit" && cd "$WORK/nogit" || exit 1
out=$(GIT_CEILING_DIRECTORIES="$WORK" sh "$CHECK" 2>&1)
rc=$?
expect 2 "outside a git repo exits 2"

# 13. the checker passes on this repository
cd "$ROOT" || exit 1
run_check
expect 0 "check passes on this repository" "OK"

# 14. LICENSE is byte-identical to the canonical Apache-2.0 text
if command -v sha256sum >/dev/null 2>&1; then
	got=$(sha256sum "$ROOT/LICENSE" | cut -d' ' -f1)
elif command -v shasum >/dev/null 2>&1; then
	got=$(shasum -a 256 "$ROOT/LICENSE" | cut -d' ' -f1)
else
	got=''
fi
if [ -z "$got" ]; then
	ok "LICENSE checksum (skipped: no sha256 tool)"
elif [ "$got" = "$LICENSE_SHA256" ]; then
	ok "LICENSE is the canonical Apache-2.0 text"
else
	ko "LICENSE sha256 $got != $LICENSE_SHA256"
fi

printf '%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
