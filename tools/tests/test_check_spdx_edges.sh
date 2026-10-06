#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Edge cases for tools/check-spdx.sh (complements test_check_spdx.sh).
# Each case builds a throwaway git repo, stages files and runs the checker. Cases print "ok", "FAIL"
# or "note" (a documented gap that does not fail the run).
# Usage: sh tools/tests/test_check_spdx_edges.sh   (from anywhere inside the repo)
set -u

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CHECK="$ROOT/tools/check-spdx.sh"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/test-check-spdx-edges.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

pass=0
fail=0
notes=0
ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
ko() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }
note() { notes=$((notes + 1)); printf 'note %s\n' "$1"; }

H='# SPDX-License-Identifier: Apache-2.0'
n=0
# case WANT_RC NAME PATH PRINTF-FORMAT-CONTENT [PATTERN-in-output]
# Builds a fresh repo with exactly one staged file and runs the checker in it.
case_() {
	want=$1 name=$2 path=$3 content=$4
	n=$((n + 1))
	R="$WORK/r$n"
	mkdir -p "$R" && cd "$R" || exit 1
	git -c init.defaultBranch=main init -q .
	mkdir -p -- "$(dirname -- "$path")"
	# shellcheck disable=SC2059
	printf "$content" >"$path"
	git add -- "$path"
	out=$(sh "$CHECK" 2>&1)
	rc=$?
	cd "$WORK" || exit 1
	if [ "$rc" -ne "$want" ]; then
		ko "$name (exit $rc, want $want): $out"
	elif [ $# -ge 5 ] && ! printf '%s\n' "$out" | grep -F -q -- "$5"; then
		ko "$name (output lacks '$5'): $out"
	else
		ok "$name"
	fi
}

# Line endings, position, preamble
case_ 0 "CRLF line endings pass" crlf.py "$H\r\nprint(1)\r\n"
case_ 1 "CRLF file with the header on line 11 fails" crlf11.py \
	"a=1\r\na=2\r\na=3\r\na=4\r\na=5\r\na=6\r\na=7\r\na=8\r\na=9\r\na=10\r\n$H\r\n" crlf11.py
case_ 0 "header ending in a lone CR at EOF passes" cr.py "$H\r"
case_ 0 "shebang + header passes (.sh)" run.sh "#!/bin/sh\n$H\necho hi\n"
case_ 0 "shebang + coding line + header passes (.py)" c.py "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n$H\n"
case_ 0 "UTF-8 BOM before the header passes" bom.py "\357\273\277$H\n"
case_ 0 "header without trailing newline passes" nonl.py "$H"
case_ 1 "header on line 11 after a shebang fails" late.sh \
	"#!/bin/sh\n#\n#\n#\n#\n#\n#\n#\n#\n#\n$H\n" late.sh
case_ 0 "block comment on one line (/* ... */) passes" a.kt "/* SPDX-License-Identifier: Apache-2.0 */\npackage a\n"
case_ 0 "Gradle Kotlin DSL // header passes" build.gradle.kts "// SPDX-License-Identifier: Apache-2.0\nplugins {}\n"

# Empty files
case_ 1 "empty .py fails (policy: every source file needs the header)" empty.py "" empty.py
case_ 1 "empty __init__.py fails (needs the header too)" pkg/__init__.py "" "pkg/__init__.py"
case_ 1 "empty Makefile fails" Makefile "" Makefile

# Nested directories
case_ 0 "deeply nested file with header passes" a/b/c/d/e/f.py "$H\n"
case_ 1 "deeply nested header-less file fails and is named" a/b/c/d/e/f.py "x=1\n" "a/b/c/d/e/f.py"
case_ 1 "nested mk fragment without header fails" sub/mk/x.mk ".PHONY: x\n" "sub/mk/x.mk"
case_ 1 "nested .gradle without header fails" android/app/build.gradle "plugins {}\n" "android/app/build.gradle"
case_ 0 "a nested dir named .team that is not top-level is still checked (header present)" x/.team/a.py "$H\n"
case_ 1 "a nested dir named .team that is not top-level is still checked (no header)" x/.team/a.py "x=1\n" "x/.team/a.py"

# Non-Apache and malformed identifiers
case_ 1 "MIT fails" mit.py "# SPDX-License-Identifier: MIT\n" mit.py
case_ 1 "GPL-2.0-only fails" gpl.sh "#!/bin/sh\n# SPDX-License-Identifier: GPL-2.0-only\n" gpl.sh
case_ 1 "'MIT OR Apache-2.0' (Apache not first) fails" dual.py "# SPDX-License-Identifier: MIT OR Apache-2.0\n" dual.py
case_ 1 "lower-case tag fails" lc.py "# spdx-license-identifier: apache-2.0\n" lc.py
case_ 1 "missing space after the colon fails" ns.py "# SPDX-License-Identifier:Apache-2.0\n" ns.py
case_ 1 "Apache-2.0-only look-alike fails" only.py "# SPDX-License-Identifier: Apache-2.0-only\n" only.py
case_ 0 "'Apache-2.0 WITH LLVM-exception' passes (starts with Apache-2.0)" w.py \
	"# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception\n"

# File names
case_ 1 "non-ASCII path is checked and named" "é/ü.py" "x=1\n" "é/ü.py"
case_ 1 "backslash in a name is refused (git quotes it)" 'back\\slash.py' "$H\n" "unusual file name"
case_ 1 "leading dash in a name is checked" "-n.py" "x=1\n" "-n.py"
case_ 0 "upper-case extension is not covered (X.PY)" X.PY "x=1\n"

# Repository states
R="$WORK/corrupt"
mkdir -p "$R" && cd "$R" && git init -q . && printf 'x=1\n' >a.py && git add a.py && printf 'garbage\n' >.git/index
out=$(sh "$CHECK" 2>&1)
rc=$?
cd "$WORK" || exit 1
[ "$rc" -eq 2 ] && ok "corrupt index fails closed (exit 2)" || ko "corrupt index (exit $rc): $out"

R="$WORK/subdir"
mkdir -p "$R/x/y" && cd "$R" && git init -q . && printf 'x=1\n' >a.py && git add a.py && cd x/y || exit 1
out=$(sh "$CHECK" 2>&1)
rc=$?
cd "$WORK" || exit 1
[ "$rc" -eq 1 ] && printf '%s\n' "$out" | grep -q 'a\.py' && ok "run from a subdirectory checks the whole repo" ||
	ko "run from a subdirectory (exit $rc): $out"

R="$WORK/symlink"
mkdir -p "$R/.team" && cd "$R" && git init -q . && printf 'x=1\n' >.team/s.py && ln -s .team/s.py tool.py &&
	git add . || exit 1
out=$(sh "$CHECK" 2>&1)
rc=$?
cd "$WORK" || exit 1
if [ "$rc" -eq 0 ]; then
	note "tracked symlink tool.py -> exempt header-less .team/s.py passes unchecked: $out"
else
	ok "symlink to an exempt header-less file is caught"
fi

printf '%d passed, %d failed, %d notes\n' "$pass" "$fail" "$notes"
[ "$fail" -eq 0 ]
