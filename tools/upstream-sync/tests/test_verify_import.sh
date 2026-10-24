#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Tests of tools/upstream-sync/verify-import.sh (T1.84), offline: each case copies the vendored module into a
# throwaway git repository, plants one defect and requires the exact exit code (1 = mismatch, 2 = missing input or
# usage) and the reason in the output, so a case cannot pass for the wrong reason. The pristine module must pass in
# the work-tree mode and in the --rev mode. The mode that compares with an upstream checkout needs a local checkout
# and is run by hand (see tools/README.md).
# Usage: sh tools/upstream-sync/tests/test_verify_import.sh   (from anywhere inside the repo)
set -u

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
VERIFY=$ROOT/tools/upstream-sync/verify-import.sh
MOD=android/third_party/g2-driver
SRC=$MOD/src/main/kotlin/app/hudcompanion/g2driver
G2=$SRC/sgcs/G2.kt
TEXT=$SRC/utils/G2Text.kt

WORK=$(mktemp -d "${TMPDIR:-/tmp}/test-verify-import.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' HUP INT TERM
export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_CEILING_DIRECTORIES="$WORK"
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com

pass=0
fail=0
ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
ko() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }

[ -f "$VERIFY" ] && [ -d "$ROOT/$MOD" ] || { echo "missing $VERIFY or $ROOT/$MOD" >&2; exit 1; }
uprefix=$(sed -n 's/^upstream-prefix: //p' "$ROOT/$MOD/UPSTREAM.md" | sed -n 1p)
oprefix=$(sed -n 's/^our-prefix: //p' "$ROOT/$MOD/UPSTREAM.md" | sed -n 1p)
[ -n "$uprefix" ] && [ -n "$oprefix" ] || { echo "UPSTREAM.md lacks the prefix fields" >&2; exit 1; }

# fresh -> $S: a new repository holding the pristine module, committed once; cwd = $S
n=0
fresh() {
	n=$((n + 1))
	S=$WORK/r$n
	mkdir -p "$S/$MOD" && cp -R "$ROOT/$MOD/." "$S/$MOD/" || exit 1
	cd "$S" || exit 1
	git -c init.defaultBranch=main init -q . && git add -- "$MOD" && git commit -q -m base || exit 1
}

# edit FILE SED-EXPRESSION: rewrite a file of the scratch repository (portable, no sed -i)
edit() {
	sed "$2" "$1" >"$1.new" && mv "$1.new" "$1"
}

# run ARGS...: run the script in the current scratch repository; sets $rc and $out
run() {
	out=$(sh "$VERIFY" "$@" 2>&1)
	rc=$?
}

# expect RC NAME [TEXT-in-output]
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

# --- the pristine import passes, in both modes
fresh
run
expect 0 "pristine module: work-tree mode" "OK (4 files)"
run --rev HEAD
expect 0 "pristine module: --rev mode" "OK (4 files)"

# --- the forward mapping applied in full: an import or package line left with the upstream prefix fails
fresh
awk -v o="$oprefix" -v u="$uprefix" '!d && $1 == "import" && index($2, o) == 1 { $2 = u substr($2, length(o) + 1); d = 1 } { print }' "$G2" >"$G2.new" && mv "$G2.new" "$G2"
run
expect 1 "an import left with the upstream prefix fails" "upstream prefix is still in our file"
fresh
edit "$TEXT" "s/^package $oprefix/package $uprefix/"
run
expect 1 "a package line left with the upstream prefix fails" "upstream prefix is still in our file"
fresh
printf 'val leftover = "%s"\n' "$uprefix" >>"$G2"
run
expect 1 "the upstream prefix anywhere else in the body fails" "upstream prefix is still in our file"
fresh
printf 'val ours = "%s"\n' "$oprefix" >>"$G2"
run
expect 1 "our prefix outside package/import lines fails" "our prefix occurs outside package/import lines"

# --- bytes: body, header lines, licence, recorded hash
fresh
printf '// one more line\n' >>"$G2"
run
expect 1 "a changed body fails the hash" "differs from the recorded"
fresh
edit "$G2" '2s/modified by/changed by/'
run
expect 1 "an altered Copied-from line fails" "header lines differ"
fresh
edit "$G2" '1s/Apache-2.0/MIT/'
run
expect 1 "an altered SPDX line fails" "header lines differ"
fresh
printf 'x\n' >>"$MOD/LICENSE"
run
expect 1 "a changed LICENSE fails" "LICENSE: sha256"
fresh
hash=$(grep 'utils/G2Text.kt` |' "$MOD/UPSTREAM.md" | sed 's/.*| `\([0-9a-f]*\)` |$/\1/')
case "$hash" in 0*) flip=1 ;; *) flip=0 ;; esac
edit "$MOD/UPSTREAM.md" "s/$hash/$flip${hash#?}/"
run
expect 1 "an altered recorded hash fails" "differs from the recorded"

# --- nothing else came along: extra file, module-root file, symbolic link, listed file as a link
fresh
printf 'class Extra\n' >"$SRC/Extra.kt"
run
expect 1 "an extra file under src/ fails" "differ from the table"
fresh
printf '<manifest/>\n' >"$MOD/AndroidManifest.xml"
run
expect 1 "a stray file at the module root fails" "differ from the table"
fresh
printf '<manifest/>\n' >"$MOD/AndroidManifest.xml"
git add -- "$MOD" && git commit -q -m stray
run --rev HEAD
expect 1 "a stray file at the module root fails in --rev mode" "differ from the table"
fresh
ln -s G2Text.kt "$SRC/utils/Link.kt"
run
expect 1 "a symbolic link under src/ fails" "differ from the table"
fresh
mv "$TEXT" "$TEXT.real" && ln -s G2Text.kt.real "$TEXT"
run
expect 1 "a listed file replaced by a symbolic link fails" "symbolic link"
fresh
rm "$MOD/build.gradle.kts"
run
expect 1 "a missing own file fails" "differ from the table"

# --- --rev reads the commit, not the work tree
fresh
printf '// work tree only\n' >>"$G2"
run --rev HEAD
expect 0 "--rev ignores a later work-tree change" "OK (4 files)"
run
expect 1 "the same change fails in work-tree mode" "differs from the recorded"
git add -- "$G2" && git commit -q -m bad
run --rev HEAD
expect 1 "--rev fails on a bad committed change" "differs from the recorded"
run --rev HEAD~1
expect 0 "--rev passes on the earlier good commit" "OK (4 files)"

# --- missing input and usage fail closed with exit 2
fresh
edit "$MOD/UPSTREAM.md" '/^our-prefix: /d'
run
expect 2 "a missing machine-read field exits 2" "field is missing"
fresh
edit "$MOD/UPSTREAM.md" 's/^upstream-commit: .*/upstream-commit: nothex/'
run
expect 2 "a non-hex upstream commit exits 2" "not a hex hash"
fresh
run --rev deadbeef
expect 2 "an unknown --rev exits 2" "not a commit"
run --rev
expect 2 "--rev without a value exits 2" "needs a commit"
run --upstream "$WORK/nonexistent"
expect 2 "--upstream with a missing directory exits 2" "not a directory"
mkdir "$WORK/emptyup" && git -C "$WORK/emptyup" -c init.defaultBranch=main init -q .
run --upstream "$WORK/emptyup"
expect 2 "--upstream without the pinned commit exits 2" "does not hold commit"
run --bogus
expect 2 "an unknown option exits 2" "usage"
mkdir "$WORK/plain" && cd "$WORK/plain" || exit 1
run
expect 2 "outside a git repository exits 2" "not inside a git repository"

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
