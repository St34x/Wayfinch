#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Point the test-Hermes pair (clone + venv) of one host at another pin, or back. Used at the merge of a pin bump
# (docs/test-hermes.md, "Moving the pin"). Never touches anything but the two links, the two renames of a first
# swap and the file pin-previous, all inside ONE directory: TEST_HERMES_LOCAL (default <repo>/.local).
#   sh tools/test-hermes/swap-pin.sh [-n] swap <new> [<old>]
#       <new>  short name of the pair to use: <root>/hermes-agent-<new> (clone) + <root>/venv314-<new> (venv).
#       <old>  only when hermes-agent and venv314 are still plain directories (first swap on a host): they are
#              renamed to hermes-agent-<old> / venv314-<old> and kept. Refused when they are links already.
#   sh tools/test-hermes/swap-pin.sh [-n] rollback
#       back to the pair the last swap/rollback replaced (recorded in <root>/pin-previous); a second rollback
#       undoes the first.
#   -n  check everything and print the plan, change nothing.
# Refuses (exit 2, nothing changed) when the target pair is missing or incomplete (clone without .git, venv
# without bin/python or pyvenv.cfg, either one a link), when hermes-agent and venv314 are not both plain
# directories or both links of the form hermes-agent-<x> / venv314-<x> with the same <x>, or on a bad name.
# The links are replaced with rename(2) (ln -s + mv -T: a reader sees the old or the new target, never none);
# the two links are two renames, so no test run may be in progress: hold every lock that serialises the runs of
# the host while swapping. Needs mv -T (GNU coreutils, busybox) and readlink.
set -eu
PROJ=$(cd "$(dirname "$0")/../.." && pwd)
P="${TEST_HERMES_LOCAL:-$PROJ/.local}"
AG=hermes-agent
VE=venv314
PREV=pin-previous
DRY=0
die() {
	echo "swap-pin: $*" >&2
	exit 2
}
say() { echo "swap-pin: $*"; }
usage() { die "usage: swap-pin.sh [-n] swap <new> [<old>] | swap-pin.sh [-n] rollback (root: TEST_HERMES_LOCAL, now $P)"; }
# is_short NAME: a plain word (letters, digits, '_' and '-', not starting with '-').
is_short() {
	case "$1" in '' | -* | *[!A-Za-z0-9_-]*) return 1 ;; esac
	return 0
}
# kind PATH: link | dir | other | none (a dangling link is a link).
kind() {
	if [ -L "$1" ]; then echo link
	elif [ -d "$1" ]; then echo dir
	elif [ -e "$1" ]; then echo other
	else echo none; fi
}
# link_short LINK PREFIX: the <x> of a link whose target is exactly PREFIX-<x>; fails otherwise.
link_short() {
	t=$(readlink "$1")
	case "$t" in "$2"-*) ;; *) return 1 ;; esac
	t=${t#"$2"-}
	is_short "$t" || return 1
	echo "$t"
}
# pair_ok SHORT: the pair for SHORT is complete (real directories, not links).
pair_ok() {
	[ ! -L "$AG-$1" ] && [ -d "$AG-$1" ] || return 1
	[ -e "$AG-$1/.git" ] || return 1
	[ ! -L "$VE-$1" ] && [ -d "$VE-$1" ] || return 1
	[ -x "$VE-$1/bin/python" ] && [ -f "$VE-$1/pyvenv.cfg" ] || return 1
}

[ "${1:-}" = "-n" ] && DRY=1 && shift
MODE=${1:-}
[ $# -gt 0 ] && shift
case "$MODE:$#" in
swap:1 | swap:2) NEW=$1 OLD=${2:-} ;;
rollback:0) NEW= OLD= ;;
*) usage ;;
esac
case "$P" in /*) ;; *) P=$(pwd)/$P ;; esac
[ -d "$P" ] || die "no such directory: $P (TEST_HERMES_LOCAL)"
cd "$P"

if [ "$MODE" = rollback ]; then
	[ -f "$PREV" ] || die "nothing to roll back: no $P/$PREV (no swap was done with this script)"
	NEW=$(cat "$PREV")
fi
is_short "$NEW" || die "bad name for the new pair: '$NEW'"
[ -z "$OLD" ] || is_short "$OLD" || die "bad name for the old pair: '$OLD'"
pair_ok "$NEW" || die "the pair $AG-$NEW + $VE-$NEW in $P is missing or incomplete (clone with .git, venv with bin/python and pyvenv.cfg; real directories)"

STATE=$(kind "$AG")/$(kind "$VE")
CUR=
case "$STATE" in
link/link)
	[ -z "$OLD" ] || die "$AG and $VE are links already: the old pair is kept as it is, do not pass <old>"
	CUR=$(link_short "$AG" "$AG") || die "$AG is a link to something other than $AG-<name> (or a dangling one): $(readlink "$AG")"
	c2=$(link_short "$VE" "$VE") || die "$VE is a link to something other than $VE-<name>: $(readlink "$VE")"
	[ "$CUR" = "$c2" ] || die "$AG points at $AG-$CUR but $VE at $VE-$c2: fix by hand first"
	;;
dir/dir)
	[ "$MODE" = swap ] || die "rollback needs links, but $AG and $VE are plain directories (no swap was done)"
	[ -n "$OLD" ] || die "$AG and $VE are plain directories: name them with <old> (swap $NEW <old>) so that they are kept"
	[ "$OLD" != "$NEW" ] || die "<old> and <new> are the same: $NEW"
	for n in "$AG-$OLD" "$VE-$OLD"; do
		[ "$(kind "$n")" = none ] || die "$P/$n exists already: cannot keep the old pair under that name"
	done
	;;
none/none)
	[ "$MODE" = swap ] || die "rollback needs links, but $AG and $VE do not exist"
	[ -z "$OLD" ] || die "$AG and $VE do not exist: nothing to keep, do not pass <old>"
	;;
*) die "$AG and $VE must be both plain directories, both links or both absent (found $STATE): fix by hand first" ;;
esac

if [ "$CUR" = "$NEW" ]; then
	say "already on $NEW ($AG -> $AG-$NEW, $VE -> $VE-$NEW): nothing to do"
	exit 0
fi
case "$STATE" in
link/link) PLAN="repoint $AG and $VE from $CUR to $NEW; $PREV = $CUR" ;;
dir/dir) PLAN="rename $AG and $VE to $AG-$OLD and $VE-$OLD, link them to $NEW; $PREV = $OLD" ;;
*) PLAN="create the links $AG and $VE to $NEW" ;;
esac
if [ "$DRY" -eq 1 ]; then
	say "dry run in $P: would $PLAN"
	exit 0
fi

T="swap.$$"
trap 'rm -f "$AG.$T" "$VE.$T" "$PREV.$T"' EXIT
trap 'exit 1' INT TERM
# Stage everything that can fail before the first change.
ln -s "$AG-$NEW" "$AG.$T"
ln -s "$VE-$NEW" "$VE.$T"
if [ "$STATE" = dir/dir ]; then
	mv -T "$AG" "$AG-$OLD"
	mv -T "$VE" "$VE-$OLD"
	CUR=$OLD
fi
mv -T "$AG.$T" "$AG"
mv -T "$VE.$T" "$VE"
if [ -n "$CUR" ]; then
	printf '%s\n' "$CUR" >"$PREV.$T"
	mv -T "$PREV.$T" "$PREV"
fi

# Check the result through the links, the way the tests will use it.
[ "$(readlink "$AG")" = "$AG-$NEW" ] && [ "$(readlink "$VE")" = "$VE-$NEW" ] && [ -e "$AG/.git" ] && [ -x "$VE/bin/python" ] ||
	{
		echo "swap-pin: result check failed in $P" >&2
		exit 1
	}
say "in $P: $PLAN"
say "now: $AG -> $(readlink "$AG"), $VE -> $(readlink "$VE")${CUR:+; roll back with: swap-pin.sh rollback}"
