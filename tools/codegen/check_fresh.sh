#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Codegen freshness (T1.104; ENGINEERING.md section 4), run by `make check-codegen`.
# Regenerates the WHOLE generated directory into a temporary one and compares it with `diff -r`: an extra (e.g.
# hand-written), missing or edited file fails. Only __pycache__ (gitignored interpreter cache) is skipped.
# Usage: sh tools/codegen/check_fresh.sh PYTHON [GENERATED_DIR]   (default: hermes-plugin/glasses/generated)
# Exit:  0 fresh, 1 stale (diff shown), 2 usage or generator error.
set -u
[ $# -ge 1 ] && [ $# -le 2 ] && [ -n "$1" ] || { echo "usage: check_fresh.sh PYTHON [GENERATED_DIR]" >&2; exit 2; }
py=$1
dir=${2:-hermes-plugin/glasses/generated}
gen=$(dirname "$0")/gen_protocol.py
tmp=$(mktemp -d) || exit 2
trap 'rm -rf "$tmp"' EXIT
"$py" "$gen" --out-dir "$tmp/generated" || exit 2
if [ ! -d "$dir" ]; then
	echo "check-codegen: $dir is missing; run: python3 tools/codegen/gen_protocol.py" >&2
	exit 1
fi
diff -r -u -x __pycache__ "$dir" "$tmp/generated" || {
	echo "check-codegen: $dir is stale (extra, missing or edited file); run: python3 tools/codegen/gen_protocol.py" >&2
	echo "  and delete any file in $dir that the generator does not write" >&2
	exit 1
}
echo "check-codegen: $dir matches the generator ($(ls "$tmp/generated" | wc -l | tr -d ' ') files)"
