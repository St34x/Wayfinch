#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Tests for tools/secret-scan.sh. Each case builds a throwaway git repo in a temp dir and scans it.
# The planted "secrets" are generated at run time from split literals, so this file itself never
# contains a value that a secret scanner would flag. They are random and were never valid credentials.
#
# Usage: sh tools/tests/test_secret_scan.sh   (from the repo root; the gitleaks binary cache under
# .local/gitleaks is shared with `make secret-scan`, so only the first run downloads).
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SCAN="$ROOT/tools/secret-scan.sh"
GITLEAKS_CACHE_DIR=${GITLEAKS_CACHE_DIR:-$ROOT/.local/gitleaks}
export GITLEAKS_CACHE_DIR

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; [ -f "$WORK/out" ] && sed 's/^/       | /' "$WORK/out"; return 0; }

# Fake GitHub-PAT-shaped token: prefix split so no literal match exists in this file; body is "FAKE"
# plus 32 random alphanumerics (enough entropy for the default rule to fire).
fake_token() {
	body=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | dd bs=32 count=1 2>/dev/null)
	printf '%s%s%s' 'gh' 'p_' "FAKE$body"
}

new_repo() {
	r="$WORK/$1"
	mkdir -p "$r"
	git -C "$r" init -q
	cp "$ROOT/.gitleaks.toml" "$r/.gitleaks.toml"
	printf '.local/\n' >"$r/.gitignore"
	printf 'hello\n' >"$r/README.md"
	git -C "$r" add .gitignore .gitleaks.toml README.md
	git -C "$r" -c user.name=test -c user.email=test@example.invalid commit -q -m init
	echo "$r"
}

run_scan() { # run_scan <args...>; sets RC, output in $WORK/out
	set +e
	sh "$SCAN" "$@" >"$WORK/out" 2>&1
	RC=$?
	set -e
}

# 1. Clean repo passes.
r=$(new_repo clean)
run_scan "$r"
[ "$RC" -eq 0 ] && ok "clean repo passes" || bad "clean repo passes (rc=$RC)"

# 2. Untracked (not ignored) file with a fake token fails; the token is redacted from output.
r=$(new_repo planted)
tok=$(fake_token)
printf 'api_token = "%s"\n' "$tok" >"$r/config.txt"
run_scan "$r"
[ "$RC" -eq 1 ] && grep -q 'leaks found' "$WORK/out" && ok "planted fake key in working tree fails (rc 1)" ||
	bad "planted fake key in working tree fails (rc=$RC)"
grep -q "$tok" "$WORK/out" && bad "token is redacted in output" || ok "token is redacted in output"
# Verbose report names file, line and rule, with a path relative to the repo (not the temp copy).
grep -q '^File: *config.txt$' "$WORK/out" && grep -q '^Line: *1$' "$WORK/out" && grep -q '^RuleID:' "$WORK/out" &&
	ok "report names file, line and rule (relative path)" || bad "report names file, line and rule"
grep -q '^Fingerprint: *config.txt:' "$WORK/out" && ok "tree fingerprint is relative to the repo" ||
	bad "tree fingerprint is relative to the repo"

# 3. A key that was committed and later deleted is caught by the history scan only.
r=$(new_repo history)
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/old.txt"
git -C "$r" add old.txt
git -C "$r" -c user.name=test -c user.email=test@example.invalid commit -q -m leak
git -C "$r" rm -q old.txt
git -C "$r" -c user.name=test -c user.email=test@example.invalid commit -q -m remove
run_scan --history "$r"
[ "$RC" -ne 0 ] && ok "deleted key still found in history" || bad "deleted key still found in history (rc=$RC)"
run_scan --tree "$r"
[ "$RC" -eq 0 ] && ok "--tree ignores history" || bad "--tree ignores history (rc=$RC)"
run_scan "$r"
[ "$RC" -ne 0 ] && ok "default mode includes history" || bad "default mode includes history (rc=$RC)"

# 4. Git-ignored files (e.g. .local/ with real local tokens) are not scanned.
r=$(new_repo ignored)
mkdir -p "$r/.local"
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/.local/token.txt"
run_scan "$r"
[ "$RC" -eq 0 ] && ok "ignored .local/ is not scanned" || bad "ignored .local/ is not scanned (rc=$RC)"

# 5. Allowlisted fixture directory (see .gitleaks.toml) may hold fake keys.
r=$(new_repo fixture)
mkdir -p "$r/pkg/tests/fixtures/fake-secrets"
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/pkg/tests/fixtures/fake-secrets/token.txt"
run_scan "$r"
[ "$RC" -eq 0 ] && ok "tests/fixtures/fake-secrets/ is allowlisted" ||
	bad "tests/fixtures/fake-secrets/ is allowlisted (rc=$RC)"
# ...but the same key one directory up is not.
mv "$r/pkg/tests/fixtures/fake-secrets/token.txt" "$r/pkg/tests/fixtures/token.txt"
run_scan "$r"
[ "$RC" -ne 0 ] && ok "allowlist is narrow" || bad "allowlist is narrow (rc=$RC)"

# 6. A cached archive with the wrong checksum is refused (no download, no execution).
r=$(new_repo tamper)
bogus="$WORK/bogus-cache"
mkdir -p "$bogus"
for p in linux_x64 linux_arm64 darwin_x64 darwin_arm64; do
	printf 'not gitleaks\n' >"$bogus/gitleaks_$(sed -n 's/^GITLEAKS_VERSION=//p' "$SCAN")_$p.tar.gz"
done
set +e
GITLEAKS_CACHE_DIR="$bogus" sh "$SCAN" "$r" >"$WORK/out" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] && grep -qi 'checksum mismatch' "$WORK/out" && ok "tampered cached archive is refused" ||
	bad "tampered cached archive is refused (rc=$RC)"

# 7. Pinned version: the binary that runs is the pinned one.
run_scan --version
pinned=$(sed -n 's/^GITLEAKS_VERSION=//p' "$SCAN")
[ "$RC" -eq 0 ] && grep -qx "$pinned" "$WORK/out" && ok "runs pinned gitleaks $pinned" ||
	bad "runs pinned gitleaks $pinned (rc=$RC)"
# ...and leaves no temp dir (with the extracted binary) behind.
mkdir -p "$WORK/tmpdir"
set +e
TMPDIR="$WORK/tmpdir" sh "$SCAN" --version >"$WORK/out" 2>&1
RC=$?
set -e
[ "$RC" -eq 0 ] && [ -z "$(ls -A "$WORK/tmpdir")" ] && ok "--version cleans up its temp dir" ||
	bad "--version cleans up its temp dir (rc=$RC, left: $(ls -A "$WORK/tmpdir"))"

# 8. A gitleaks failure that is not a leak (broken config) is a tool error (rc 2), not "leaks found".
r=$(new_repo brokencfg)
printf 'this is [not valid toml\n' >"$r/.gitleaks.toml"
run_scan --tree "$r"
[ "$RC" -eq 2 ] && grep -q 'gitleaks failed' "$WORK/out" && ! grep -q 'leaks found (' "$WORK/out" &&
	ok "broken config is a tool error (rc 2), not a leak" || bad "broken config is a tool error (rc=$RC)"

# 9. .gitleaksignore: a fingerprint copied from a tree report suppresses exactly that finding.
r=$(new_repo ignorefile)
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/fp.txt"
run_scan --tree "$r"
sed -n 's/^Fingerprint: *//p' "$WORK/out" >"$r/.gitleaksignore"
if [ "$RC" -eq 1 ] && [ -s "$r/.gitleaksignore" ]; then
	run_scan --tree "$r"
	[ "$RC" -eq 0 ] && ok ".gitleaksignore fingerprint from a tree report matches the next run" ||
		bad ".gitleaksignore fingerprint from a tree report matches the next run (rc=$RC)"
	printf 'api_token = "%s"\n' "$(fake_token)" >"$r/other.txt"
	run_scan --tree "$r"
	[ "$RC" -eq 1 ] && grep -q '^File: *other.txt$' "$WORK/out" && ok ".gitleaksignore does not hide other findings" ||
		bad ".gitleaksignore does not hide other findings (rc=$RC)"
else
	bad ".gitleaksignore: first scan gave no fingerprint (rc=$RC)"
fi

# 10. Fail closed: a file that cannot be read makes the scan fail (rc 2), never a partial "no leaks".
if [ "$(id -u)" -ne 0 ]; then
	r=$(new_repo unreadable)
	printf 'api_token = "%s"\n' "$(fake_token)" >"$r/locked.txt"
	chmod 000 "$r/locked.txt"
	run_scan --tree "$r"
	chmod 600 "$r/locked.txt"
	[ "$RC" -eq 2 ] && grep -q 'cannot copy' "$WORK/out" && ! grep -q 'no leaks found' "$WORK/out" &&
		ok "unreadable file fails the scan (rc 2)" || bad "unreadable file fails the scan (rc=$RC)"
else
	echo "skip - unreadable file (running as root)"
fi

# 11. Fail closed: not a git work tree -> rc 2 in every mode.
mkdir -p "$WORK/ceil/plain"
printf 'hello\n' >"$WORK/ceil/plain/a.txt"
for m in --tree --history --all; do
	set +e
	GIT_CEILING_DIRECTORIES="$WORK/ceil" sh "$SCAN" $m "$WORK/ceil/plain" >"$WORK/out" 2>&1
	RC=$?
	set -e
	[ "$RC" -eq 2 ] && grep -q 'not a git work tree' "$WORK/out" && ok "non-git dir fails closed ($m)" ||
		bad "non-git dir fails closed ($m, rc=$RC)"
done

# 12. An explicit DIR must be the top of its work tree: a subdir is refused (rc 2), never mapped to the top.
r=$(new_repo parent)
printf 'sub/\n' >>"$r/.gitignore"
mkdir -p "$r/sub" "$r/tracked"
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/sub/config.txt"
printf 'hello\n' >"$r/tracked/a.txt"
run_scan --tree "$r/sub"
[ "$RC" -eq 2 ] && grep -q 'is not the top of a git work tree' "$WORK/out" && ! grep -q 'no leaks found' "$WORK/out" &&
	ok "gitignored subdir is refused (rc 2)" || bad "gitignored subdir is refused (rc=$RC)"
run_scan --tree "$r/tracked"
[ "$RC" -eq 2 ] && grep -q 'is not the top of a git work tree' "$WORK/out" && ok "tracked subdir is refused (rc 2)" ||
	bad "tracked subdir is refused (rc=$RC)"
run_scan --history "$r/tracked"
[ "$RC" -eq 2 ] && ok "subdir is refused in --history mode too" || bad "subdir refused in --history (rc=$RC)"
# The repo top itself still scans (sub/ is ignored there, so plant a non-ignored token to prove it reads files).
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/top.txt"
run_scan --tree "$r"
[ "$RC" -eq 1 ] && grep -q '^File: *top.txt$' "$WORK/out" && ok "repo top still scans (rc 1)" ||
	bad "repo top still scans (rc=$RC)"
# A directory that is its own repo nested inside another repo scans itself.
n=$(new_repo parent/sub/nested)
printf 'api_token = "%s"\n' "$(fake_token)" >"$n/inner.txt"
run_scan --tree "$n"
[ "$RC" -eq 1 ] && grep -q '^File: *inner.txt$' "$WORK/out" && ! grep -q 'top.txt' "$WORK/out" &&
	ok "nested own repo scans itself" || bad "nested own repo scans itself (rc=$RC)"
# Without an argument the scan is still "top of the cwd's work tree", also from a subdir.
set +e
(cd "$r/tracked" && sh "$SCAN" --tree) >"$WORK/out" 2>&1
RC=$?
set -e
[ "$RC" -eq 1 ] && grep -q '^File: *top.txt$' "$WORK/out" && ok "no argument from a subdir scans the work-tree top" ||
	bad "no argument from a subdir scans the work-tree top (rc=$RC)"

echo "test_secret_scan: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
