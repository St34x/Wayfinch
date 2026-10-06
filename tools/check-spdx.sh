#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# check-spdx.sh — every tracked source file must carry
#   SPDX-License-Identifier: Apache-2.0
# in its first HEAD_LINES lines (project licence: LICENSE).
#
# Covered: *.py *.kt *.kts *.sh *.mk Makefile (also GNUmakefile/makefile) *.gradle
#          (*.gradle.kts is covered by *.kts).
# Exempt:  everything else — data (JSON, YAML, properties, fixtures), Markdown, LICENSE/NOTICE,
#          the Gradle wrapper's own files (gradlew, gradlew.bat, gradle-wrapper.*), and the private
#          team folder .team/ (never published). Untracked files are not checked.
# A compound expression that starts with Apache-2.0 (e.g. "Apache-2.0 AND MIT" for ported code) passes.
#
# Usage: sh tools/check-spdx.sh   (from anywhere inside the repository)
# Exit:  0 all covered files have the header, 1 offenders found (listed on stderr), 2 usage error.
set -u

HEAD_LINES=10
PATTERN='SPDX-License-Identifier: Apache-2\.0([[:space:]]|\*/|$)'

top=$(git rev-parse --show-toplevel 2>/dev/null) || {
	echo "check-spdx: not inside a git repository" >&2
	exit 2
}
cd "$top" || exit 2

list=$(git -c core.quotePath=false ls-files) || {
	echo "check-spdx: git ls-files failed" >&2
	exit 2
}

checked=0
bad=0
# Here-document instead of a pipe so the counters survive the loop (POSIX sh).
while IFS= read -r f; do
	[ -n "$f" ] || continue
	case "$f" in
	.team/*) continue ;;
	\"*)
		# git quotes names with control characters, quotes or backslashes; refuse them.
		echo "check-spdx: unusual file name, cannot check: $f" >&2
		bad=$((bad + 1))
		continue
		;;
	esac
	case "${f##*/}" in
	*.py | *.kt | *.kts | *.sh | *.mk | *.gradle | Makefile | GNUmakefile | makefile) ;;
	*) continue ;;
	esac
	if [ -L "$f" ]; then
		continue # symlink: its target is checked where it is tracked
	fi
	if [ ! -e "$f" ]; then
		continue # tracked but deleted from the work tree
	fi
	checked=$((checked + 1))
	if ! head -n "$HEAD_LINES" <"$f" | grep -E -q -- "$PATTERN"; then
		echo "check-spdx: missing 'SPDX-License-Identifier: Apache-2.0' in the first $HEAD_LINES lines: $f" >&2
		bad=$((bad + 1))
	fi
done <<EOF
$list
EOF

if [ "$bad" -ne 0 ]; then
	echo "check-spdx: FAIL ($bad of $checked file(s))" >&2
	exit 1
fi
echo "check-spdx: OK ($checked file(s))"
exit 0
