#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Edge cases for tools/secret-scan.sh DIR handling: path with spaces, symlinked repo dir,
# non-git dir under GIT_CEILING_DIRECTORIES, corrupt index (must not report a vacuous clean scan),
# symlink to a subdir (must be refused like the subdir itself).
# Needs the cached gitleaks archive (<repo top>/.local/gitleaks); skips cleanly without it.
set -u
TOP=$(cd "$(dirname "$0")/../.." && pwd)
SCAN="$TOP/tools/secret-scan.sh"
CACHE=${GITLEAKS_CACHE_DIR:-$TOP/.local/gitleaks}
if ! ls "$CACHE"/gitleaks_*.tar.gz >/dev/null 2>&1; then
	echo "test_secret_scan_dir_arg: SKIP (no cached gitleaks archive in $CACHE)"
	exit 0
fi
export GITLEAKS_CACHE_DIR="$CACHE"
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT
pass=0
fail=0
ok() { pass=$((pass + 1)); echo "ok   - $1"; }
bad() { fail=$((fail + 1)); echo "FAIL - $1"; }
mkrepo() { # mkrepo <dir>
	mkdir -p "$1" && git -C "$1" init -q && echo hello >"$1/a.txt" && mkdir "$1/sub" && echo x >"$1/sub/b.txt" &&
		git -C "$1" add a.txt sub/b.txt && git -C "$1" -c user.email=q@example.invalid -c user.name=q commit -qm init
}

mkrepo "$W/with space/r"
out=$(sh "$SCAN" "$W/with space/r" 2>&1)
rc=$?
[ "$rc" -eq 0 ] && printf '%s' "$out" | grep -q 'scanned ~[1-9]' && ok "repo path with spaces scans (rc 0, bytes > 0)" ||
	bad "repo path with spaces (rc $rc): $out"

ln -s "$W/with space/r" "$W/link"
sh "$SCAN" "$W/link" >/dev/null 2>&1
rc=$?
[ "$rc" -eq 0 ] && ok "symlink to repo top scans (rc 0)" || bad "symlink to repo top rc $rc"

ln -s "$W/with space/r/sub" "$W/sublink"
sh "$SCAN" "$W/sublink" >/dev/null 2>&1
rc=$?
[ "$rc" -eq 2 ] && ok "symlink to a subdir is refused (rc 2)" || bad "symlink to a subdir rc $rc (want 2)"

mkdir -p "$W/ceil/plain"
echo x >"$W/ceil/plain/f.txt"
out=$(GIT_CEILING_DIRECTORIES="$W/ceil" sh "$SCAN" "$W/ceil/plain" 2>&1)
rc=$?
[ "$rc" -eq 2 ] && ok "non-git dir under GIT_CEILING_DIRECTORIES refused (rc 2)" ||
	bad "non-git dir rc $rc (want 2): $out"
printf '%s' "$out" | grep -q 'no leaks found' && bad "non-git dir printed 'no leaks found'" ||
	ok "non-git dir never claims 'no leaks found'"

mkrepo "$W/corrupt"
printf 'garbage\n' >"$W/corrupt/.git/index"
out=$(sh "$SCAN" --tree "$W/corrupt" 2>&1)
rc=$?
[ "$rc" -eq 2 ] && ok "corrupt index refused (rc 2)" || bad "corrupt index rc $rc (want 2): $out"
printf '%s' "$out" | grep -q 'scanned ~0 bytes' && bad "corrupt index produced a vacuous 0-byte scan" ||
	ok "corrupt index: no vacuous 0-byte scan"

echo "test_secret_scan_dir_arg: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
