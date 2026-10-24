#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# verify-import.sh — prove that the vendored driver files are the pinned upstream files plus only the documented
# changes of the import: two header lines per Kotlin file and the package/import prefix rewrite.
#
# Reads android/third_party/g2-driver/UPSTREAM.md (fields upstream-commit / upstream-prefix / our-prefix / author-line
# and the file table). Per file of kind `source`: strip the first two lines (checked to be exactly the SPDX line and the
# "Copied from <upstream path>@<commit>; modified by <author> (see UPSTREAM.md)" line), reverse the prefix mapping on
# package/import lines, require that no occurrence of our prefix is left, and compare the sha256 with the recorded
# upstream-blob hash. Kind `verbatim` (LICENSE): the file's own sha256. The upstream prefix must be gone from our
# files (a package/import line left unmapped fails), and the module must hold exactly the listed files plus
# UPSTREAM.md and build.gradle.kts, entries of any type.
# Tests: tools/upstream-sync/tests/test_verify_import.sh (make check-upstream-import).
#
# Usage: sh tools/upstream-sync/verify-import.sh [--rev <commit>] [--upstream <dir>]
#   (none)             offline: our work-tree files against the recorded hashes only.
#   --rev <commit>     read our files from that commit of this repository instead of the work tree (the import
#                      commit stays checkable after later modifications).
#   --upstream <dir>   also compare every recorded hash with `git -C <dir> show <commit>:<path>` of a local checkout
#                      of the upstream project (read-only; this script never fetches or clones).
# Exit: 0 everything matches, 1 a mismatch, 2 usage or missing input (fails closed).
set -u

MODULE=android/third_party/g2-driver
rev=""
updir=""
while [ $# -gt 0 ]; do
	case "$1" in
	--rev)
		[ $# -ge 2 ] || { echo "verify-import: --rev needs a commit" >&2; exit 2; }
		rev=$2
		shift 2
		;;
	--upstream)
		[ $# -ge 2 ] || { echo "verify-import: --upstream needs a directory" >&2; exit 2; }
		updir=$2
		shift 2
		;;
	*)
		echo "usage: verify-import.sh [--rev <commit>] [--upstream <dir>]" >&2
		exit 2
		;;
	esac
done

top=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "verify-import: not inside a git repository" >&2; exit 2; }
cd "$top" || exit 2

if command -v sha256sum >/dev/null 2>&1; then
	sha() { sha256sum "$1" | cut -d' ' -f1; }
else
	sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
fi

tmpd=${TMPDIR:-/tmp}
work=$(mktemp "$tmpd/verify-import.XXXXXX") || { echo "verify-import: cannot create a temp file" >&2; exit 2; }
work2=$(mktemp "$tmpd/verify-import.XXXXXX") || { echo "verify-import: cannot create a temp file" >&2; exit 2; }
rows=$(mktemp "$tmpd/verify-import.XXXXXX") || { echo "verify-import: cannot create a temp file" >&2; exit 2; }
trap 'rm -f "$work" "$work2" "$rows"' EXIT
trap 'exit 130' HUP INT TERM

# ours <relative path> -> writes the file's bytes to stdout (work tree, or the given commit).
ours() {
	if [ -n "$rev" ]; then
		git show "$rev:$MODULE/$1"
	else
		cat "$MODULE/$1"
	fi
}

# is_link <relative path> -> success when our entry is a symbolic link (git mode 120000 in a commit).
is_link() {
	if [ -n "$rev" ]; then
		git ls-tree "$rev" -- "$MODULE/$1" | grep -q '^120000 '
	else
		[ -L "$MODULE/$1" ]
	fi
}

if [ -n "$rev" ]; then
	git rev-parse --verify --quiet "$rev^{commit}" >/dev/null || { echo "verify-import: not a commit: $rev" >&2; exit 2; }
	prov=$(git show "$rev:$MODULE/UPSTREAM.md") || { echo "verify-import: no UPSTREAM.md in $rev" >&2; exit 2; }
else
	[ -f "$MODULE/UPSTREAM.md" ] || { echo "verify-import: $MODULE/UPSTREAM.md missing" >&2; exit 2; }
	prov=$(cat "$MODULE/UPSTREAM.md")
fi

field() { printf '%s\n' "$prov" | sed -n "s/^$1: //p" | sed -n '1p'; }
commit=$(field upstream-commit)
uprefix=$(field upstream-prefix)
oprefix=$(field our-prefix)
author=$(field author-line)
for v in "$commit" "$uprefix" "$oprefix" "$author"; do
	[ -n "$v" ] || { echo "verify-import: a machine-read field is missing in UPSTREAM.md" >&2; exit 2; }
done
case "$commit" in
*[!0-9a-f]* | "") echo "verify-import: upstream-commit is not a hex hash" >&2; exit 2 ;;
esac
[ "${#commit}" -eq 40 ] || { echo "verify-import: upstream-commit is not 40 hex digits" >&2; exit 2; }

# Table rows: | `our file` | `upstream path` | kind | `sha256` |  ->  our<TAB>upstream<TAB>kind<TAB>sha
printf '%s\n' "$prov" | awk -F'|' '
NF == 6 && $2 ~ /^ *`/ {
	for (i = 2; i <= 5; i++) { gsub(/[ `]/, "", $i) }
	print $2 "\t" $3 "\t" $4 "\t" $5
}' >"$rows"
[ -s "$rows" ] || { echo "verify-import: no file table rows found in UPSTREAM.md" >&2; exit 2; }

if [ -n "$updir" ]; then
	[ -d "$updir" ] || { echo "verify-import: --upstream is not a directory: $updir" >&2; exit 2; }
	[ "$(git -C "$updir" rev-parse "$commit^{commit}" 2>/dev/null)" = "$commit" ] ||
		{ echo "verify-import: $updir does not hold commit $commit" >&2; exit 2; }
fi

bad=0
n=0
tab=$(printf '\t')
while IFS="$tab" read -r f upath kind want; do
	n=$((n + 1))
	if is_link "$f"; then
		echo "verify-import: FAIL $f: is a symbolic link" >&2
		bad=$((bad + 1))
		continue
	fi
	if ! ours "$f" >"$work" 2>/dev/null; then
		echo "verify-import: FAIL $f: cannot read our file" >&2
		bad=$((bad + 1))
		continue
	fi
	case "$kind" in
	verbatim)
		got=$(sha "$work")
		;;
	source)
		want_l1="// SPDX-License-Identifier: Apache-2.0"
		want_l2="// Copied from $upath@$commit; modified by $author (see UPSTREAM.md)"
		l1=$(sed -n '1p' "$work")
		l2=$(sed -n '2p' "$work")
		if [ "$l1" != "$want_l1" ] || [ "$l2" != "$want_l2" ]; then
			echo "verify-import: FAIL $f: the two header lines differ from the documented ones" >&2
			bad=$((bad + 1))
			continue
		fi
		# The forward mapping must be applied in full: the upstream prefix may occur nowhere in our file after
		# the header (reversing would leave it as it is and the hash would still match).
		if tail -n +3 "$work" | grep -F -q -- "$uprefix"; then
			echo "verify-import: FAIL $f: the upstream prefix is still in our file (mapping not applied)" >&2
			bad=$((bad + 1))
			continue
		fi
		esc_o=$(printf '%s' "$oprefix" | sed 's/[.]/\\./g')
		tail -n +3 "$work" | sed -e "s/^package $esc_o/package $uprefix/" -e "s/^import $esc_o/import $uprefix/" >"$work2"
		if grep -F -q -- "$oprefix" "$work2"; then
			echo "verify-import: FAIL $f: our prefix occurs outside package/import lines (mapping not reversible)" >&2
			bad=$((bad + 1))
			continue
		fi
		got=$(sha "$work2")
		;;
	*)
		echo "verify-import: FAIL $f: unknown kind '$kind'" >&2
		bad=$((bad + 1))
		continue
		;;
	esac
	if [ "$got" != "$want" ]; then
		echo "verify-import: FAIL $f: sha256 $got differs from the recorded $want" >&2
		bad=$((bad + 1))
		continue
	fi
	if [ -n "$updir" ]; then
		if ! git -C "$updir" show "$commit:$upath" >"$work2" 2>/dev/null; then
			echo "verify-import: FAIL $f: $upath not found at $commit in $updir" >&2
			bad=$((bad + 1))
			continue
		fi
		up=$(sha "$work2")
		if [ "$up" != "$want" ]; then
			echo "verify-import: FAIL $f: upstream blob sha256 $up differs from the recorded $want" >&2
			bad=$((bad + 1))
			continue
		fi
	fi
	echo "verify-import: ok $f"
done <"$rows"

# Exactly the listed files plus our two own files, of any type (nothing else came along: no stray file at the module
# root, no symbolic link). Work-tree mode lists tracked and untracked-but-not-ignored entries.
if [ -n "$rev" ]; then
	have=$(git ls-tree -r --name-only "$rev" -- "$MODULE" | sed "s|^$MODULE/||" | sort)
else
	have=$(git ls-files --cached --others --exclude-standard -- "$MODULE" | while IFS= read -r p; do
		if [ -e "$p" ] || [ -L "$p" ]; then printf '%s\n' "$p"; fi
	done | sed "s|^$MODULE/||" | sort)
fi
listed=$( (cut -f1 "$rows"; printf '%s\n' UPSTREAM.md build.gradle.kts) | sort)
if [ "$have" != "$listed" ]; then
	echo "verify-import: FAIL files of the module differ from the table plus UPSTREAM.md and build.gradle.kts" >&2
	echo "  present: $(printf '%s' "$have" | tr '\n' ' ')" >&2
	echo "  listed:  $(printf '%s' "$listed" | tr '\n' ' ')" >&2
	bad=$((bad + 1))
fi

if [ "$bad" -ne 0 ]; then
	echo "verify-import: FAIL ($bad problem(s), $n file(s) in the table)" >&2
	exit 1
fi
echo "verify-import: OK ($n files$([ -n "$updir" ] && echo ", upstream blobs compared"))"
exit 0
