#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Edge/failure cases for tools/secret-scan.sh (complements test_secret_scan.sh).
# Fake tokens are generated at run time from split literals; nothing secret-shaped is stored here.
# Cases print "ok", "FAIL" or "note" (a documented gap/limitation that does not fail the run).
#
# Usage: sh tools/tests/test_secret_scan_edges.sh   (from the repo root; offline - uses the cached archive
# in .local/gitleaks, which `make secret-scan` creates on its first run).
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
SCAN="$ROOT/tools/secret-scan.sh"
GITLEAKS_CACHE_DIR=${GITLEAKS_CACHE_DIR:-$ROOT/.local/gitleaks}
export GITLEAKS_CACHE_DIR
VERSION=$(sed -n 's/^GITLEAKS_VERSION=//p' "$SCAN")

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
C="-c user.name=test -c user.email=test@example.invalid"

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
	git -C "$r" $C commit -q -m init
	echo "$r"
}
run_scan() {
	set +e
	sh "$SCAN" "$@" >"$WORK/out" 2>&1
	RC=$?
	set -e
}

ls "$GITLEAKS_CACHE_DIR"/gitleaks_"$VERSION"_*.tar.gz >/dev/null 2>&1 ||
	{ echo "no cached gitleaks archive in $GITLEAKS_CACHE_DIR - run 'make secret-scan' once (online)"; exit 2; }

# Q1. Key committed, then removed by `git revert`: tree is clean, history still fails.
r=$(new_repo revert)
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/a.txt"
git -C "$r" add a.txt && git -C "$r" $C commit -q -m leak
git -C "$r" $C revert --no-edit HEAD >/dev/null
run_scan --tree "$r"
[ "$RC" -eq 0 ] && ok "reverted key: --tree is clean" || bad "reverted key: --tree is clean (rc=$RC)"
run_scan "$r"
[ "$RC" -eq 1 ] && ok "reverted key: default mode fails on history" || bad "reverted key: default mode fails (rc=$RC)"

# Q2. File and directory names with spaces, a newline, a leading dash.
r=$(new_repo names)
mkdir -p "$r/dir with space"
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/dir with space/my file.txt"
run_scan --tree "$r"
[ "$RC" -eq 1 ] && ok "key in 'dir with space/my file.txt' (untracked) fails" || bad "space name (rc=$RC)"
r=$(new_repo newline)
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/$(printf 'new\nline.txt')"
run_scan --tree "$r"
[ "$RC" -eq 1 ] && ok "key in a file name containing a newline fails" || bad "newline name (rc=$RC)"
r=$(new_repo dash)
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/-rf.txt"
git -C "$r" add -- -rf.txt
run_scan --tree "$r"
[ "$RC" -eq 1 ] && ok "key in '-rf.txt' (tracked) fails" || bad "leading-dash name (rc=$RC)"

# Q3. Binary file (NUL bytes) holding a key: gitleaks skips binary files (upstream behaviour).
r=$(new_repo binary)
{ printf '\000\001\002BIN\000'; printf 'api_token = "%s"\n' "$(fake_token)"; } >"$r/blob.bin"
git -C "$r" add blob.bin && git -C "$r" $C commit -q -m bin
run_scan "$r"
if [ "$RC" -eq 0 ]; then
	note "binary file with an embedded key is NOT detected (gitleaks skips binaries; tree + history rc=0)"
else
	ok "binary file with an embedded key is detected"
fi

# Q4. Empty cache + no network: hard failure (rc 2), nothing left in the cache, scan not skipped.
r=$(new_repo offline)
mkdir -p "$WORK/empty-cache"
set +e
GITLEAKS_CACHE_DIR="$WORK/empty-cache" https_proxy=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
	sh "$SCAN" "$r" >"$WORK/out" 2>&1
RC=$?
set -e
[ "$RC" -eq 2 ] && grep -q 'download failed' "$WORK/out" && ok "offline with empty cache fails hard (rc 2)" ||
	bad "offline with empty cache fails hard (rc=$RC)"
[ -z "$(ls -A "$WORK/empty-cache")" ] && ok "failed download leaves no archive in the cache" ||
	bad "failed download leaves no archive in the cache: $(ls -A "$WORK/empty-cache")"

# Q5. Cache directory does not exist + offline: same hard failure (not a crash before the message).
set +e
GITLEAKS_CACHE_DIR="$WORK/no/such/dir" https_proxy=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 \
	sh "$SCAN" "$r" >"$WORK/out" 2>&1
RC=$?
set -e
[ "$RC" -eq 2 ] && grep -q 'download failed' "$WORK/out" && ok "missing cache dir + offline fails hard (rc 2)" ||
	bad "missing cache dir + offline (rc=$RC)"

# Q6. Tampered genuine archive (real gitleaks tarball + 1 appended byte, still extractable): refused.
mkdir -p "$WORK/tcache"
cp "$GITLEAKS_CACHE_DIR"/gitleaks_"$VERSION"_*.tar.gz "$WORK/tcache/"
for f in "$WORK"/tcache/*.tar.gz; do printf 'x' >>"$f"; done
set +e
GITLEAKS_CACHE_DIR="$WORK/tcache" sh "$SCAN" "$r" >"$WORK/out" 2>&1
RC=$?
set -e
[ "$RC" -eq 2 ] && grep -q 'checksum mismatch for cached' "$WORK/out" && ! grep -q 'INF' "$WORK/out" &&
	ok "tampered real archive refused before gitleaks runs" || bad "tampered real archive (rc=$RC)"

# Q7. Git cannot list the tree (corrupt index): the scan must not pass vacuously.
r=$(new_repo corrupt)
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/new.txt"
printf 'garbage\n' >"$r/.git/index"
run_scan "$r"
[ "$RC" -ne 0 ] && ok "corrupt index: scan fails closed" ||
	bad "corrupt index: scan fails closed (rc=$RC; key in new.txt not scanned, '0 bytes' scanned)"

# Q8. Not a git repository at all: the scan must not pass vacuously.
mkdir -p "$WORK/ceiling/nogit"
cp "$ROOT/.gitleaks.toml" "$WORK/ceiling/nogit/"
printf 'api_token = "%s"\n' "$(fake_token)" >"$WORK/ceiling/nogit/a.txt"
set +e
GIT_CEILING_DIRECTORIES="$WORK/ceiling" sh "$SCAN" "$WORK/ceiling/nogit" >"$WORK/out" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] && ok "non-git directory: scan fails closed" ||
	bad "non-git directory: scan fails closed (rc=$RC; 0 bytes scanned, history 'no commits yet')"

# Q9. Run from a subdirectory with the default REPO_DIR (.): only that subdirectory is tree-scanned.
r=$(new_repo subdir)
mkdir -p "$r/tools"
printf 'x\n' >"$r/tools/x.txt"
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/root.txt"
set +e
(cd "$r/tools" && sh "$SCAN" --tree) >"$WORK/out" 2>&1
RC=$?
set -e
if [ "$RC" -eq 0 ]; then
	note "run from a subdirectory scans only that subdirectory (key in repo root missed, rc=0)"
else
	ok "run from a subdirectory scans the whole repo"
fi

# Q10. Key staged in the index, removed from the working tree before the scan: next commit would leak it.
r=$(new_repo staged)
printf 'api_token = "%s"\n' "$(fake_token)" >"$r/s.txt"
git -C "$r" add s.txt
printf 'clean\n' >"$r/s.txt"
run_scan "$r"
if [ "$RC" -eq 0 ]; then
	note "key only in the index (worktree clean, not committed) is not scanned (rc=0)"
else
	ok "key only in the index is detected"
fi

echo "test_secret_scan_edges: $PASS passed, $FAIL failed, $NOTE notes"
[ "$FAIL" -eq 0 ]
