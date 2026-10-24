#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Tests of tools/test-hermes/swap-pin.sh on throwaway directories: first swap from plain directories, swap while
# hermes-agent/venv314 are links already (the next bump), rollback and rollback of a rollback, every refusal
# (nothing may change), dry run, and planted mutants of the script that these checks must catch.
# No Hermes, no network, no ports. Usage: sh tools/test-hermes/test_swap_pin.sh (part of make test-hermes-harness).
set -u
unset TEST_HERMES_LOCAL
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
REAL="$ROOT/tools/test-hermes/swap-pin.sh"
S=$REAL
W=$(mktemp -d)
trap 'chmod -R u+w "$W" 2>/dev/null; rm -rf "$W"' EXIT INT TERM
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "ok   $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $1"; }

# --- helpers -------------------------------------------------------------------------------------------------
newroot() { R=$(mktemp -d "$W/r.XXXXXX"); }
mkpair() { # mkpair ROOT SHORT: a complete numbered pair
	mkdir -p "$1/hermes-agent-$2/.git" "$1/venv314-$2/bin"
	echo "$2" >"$1/hermes-agent-$2/id"
	: >"$1/venv314-$2/bin/python"
	chmod +x "$1/venv314-$2/bin/python"
	: >"$1/venv314-$2/pyvenv.cfg"
}
mkplain() { # mkplain ROOT SHORT: the layout before the first swap (plain directories)
	mkdir -p "$1/hermes-agent/.git" "$1/venv314/bin"
	echo "$2" >"$1/hermes-agent/id"
	: >"$1/venv314/bin/python"
	chmod +x "$1/venv314/bin/python"
	: >"$1/venv314/pyvenv.cfg"
}
snap() { find "$1" -mindepth 1 -printf '%P %y %l\n' | sort; }
sp() { TEST_HERMES_LOCAL="$R" sh "$S" "$@" >"$W/out" 2>&1 && RC=0 || RC=$?; }
req() { "$@" || { echo "failed: $*; rc=${RC:-?} out: $(cat "$W/out" 2>/dev/null)"; return 1; }; }
refused() { # refused SNAP: rc 2 and the tree is as before
	req [ "$RC" -eq 2 ] || return 1
	req [ "$(snap "$R")" = "$1" ] || return 1
}
link_is() { [ "$(readlink "$R/hermes-agent")" = "hermes-agent-$1" ] && [ "$(readlink "$R/venv314")" = "venv314-$1" ]; }

# --- checks (each returns 0 when the script under test $S behaves) ----------------------------------------------
c_first_swap() {
	newroot && mkplain "$R" old && mkpair "$R" new
	sp swap new old
	req [ "$RC" -eq 0 ] && req link_is new || return 1
	req [ "$(cat "$R/hermes-agent-old/id")" = old ] && req [ -x "$R/venv314-old/bin/python" ] || return 1
	req [ "$(cat "$R/pin-previous")" = old ] || return 1
	req [ -z "$(find "$R" -maxdepth 1 -name '*swap.*')" ] || return 1
	req grep -q 'now: hermes-agent -> hermes-agent-new' "$W/out" || return 1
}
c_link_swap() { # the next bump: both names are links already; nothing may be moved into the pair they point at
	newroot && mkplain "$R" old && mkpair "$R" new && mkpair "$R" third
	sp swap new old
	sp swap third
	req [ "$RC" -eq 0 ] && req link_is third || return 1
	req [ ! -e "$R/hermes-agent-new/hermes-agent" ] && req [ ! -e "$R/hermes-agent-new/hermes-agent-third" ] || return 1
	req [ -z "$(find "$R" -mindepth 2 -name 'hermes-agent*' -o -mindepth 2 -name 'venv314*')" ] || return 1
	req [ "$(cat "$R/hermes-agent-new/id")" = new ] && req [ "$(cat "$R/pin-previous")" = new ] || return 1
	req [ -z "$(find "$R" -maxdepth 1 -name '*swap.*')" ] || return 1
}
c_rollback() {
	newroot && mkplain "$R" old && mkpair "$R" new
	sp swap new old
	after=$(snap "$R")
	sp rollback
	req [ "$RC" -eq 0 ] && req link_is old && req [ "$(cat "$R/pin-previous")" = new ] || return 1
	req [ "$(cat "$R/hermes-agent/id")" = old ] || return 1
	req [ ! -e "$R/hermes-agent-new/hermes-agent" ] && req [ ! -e "$R/hermes-agent-old/hermes-agent-new" ] || return 1
	sp rollback
	req [ "$RC" -eq 0 ] && req [ "$(snap "$R")" = "$after" ] || return 1
}
c_rollback_refused() {
	newroot && mkplain "$R" old && mkpair "$R" new
	s0=$(snap "$R")
	sp rollback
	refused "$s0" || return 1
	echo new >"$R/pin-previous"
	s0=$(snap "$R")
	sp rollback # plain directories and a record: still nothing to go back to
	refused "$s0" || return 1
}
c_missing_target() {
	newroot && mkplain "$R" old
	s0=$(snap "$R")
	sp swap new old
	refused "$s0" || return 1
	mkpair "$R" new && sp swap new old && mkpair "$R" third && rm -r "$R/venv314-third" && s0=$(snap "$R")
	sp swap third
	refused "$s0" || return 1
}
c_incomplete() {
	for broken in nogit nopython notexec nocfg link; do
		newroot && mkplain "$R" old && mkpair "$R" new
		case $broken in
		nogit) rmdir "$R/hermes-agent-new/.git" ;;
		nopython) rm "$R/venv314-new/bin/python" ;;
		notexec) chmod -x "$R/venv314-new/bin/python" ;;
		nocfg) rm "$R/venv314-new/pyvenv.cfg" ;;
		link) mv "$R/venv314-new" "$R/elsewhere" && ln -s elsewhere "$R/venv314-new" ;;
		esac
		s0=$(snap "$R")
		sp swap new old
		refused "$s0" || { echo "case $broken"; return 1; }
	done
}
c_state_refused() {
	newroot && mkplain "$R" old && mkpair "$R" new
	rm -r "$R/venv314" && ln -s venv314-new "$R/venv314" # a directory and a link
	s0=$(snap "$R")
	sp swap new old
	refused "$s0" || return 1
	newroot && mkpair "$R" a && mkpair "$R" b && mkpair "$R" new
	ln -s hermes-agent-a "$R/hermes-agent" && ln -s venv314-b "$R/venv314" # links to different pins
	s0=$(snap "$R")
	sp swap new
	refused "$s0" || return 1
	newroot && mkpair "$R" new && ln -s gone "$R/hermes-agent" && ln -s gone2 "$R/venv314" # dangling links
	s0=$(snap "$R")
	sp swap new
	refused "$s0" || return 1
	newroot && mkpair "$R" new && ln -s "$R/hermes-agent-new" "$R/hermes-agent" && ln -s "$R/venv314-new" "$R/venv314"
	s0=$(snap "$R") # absolute links are not ours
	sp swap new
	refused "$s0" || return 1
}
c_old_rules() {
	newroot && mkplain "$R" old && mkpair "$R" new
	s0=$(snap "$R")
	sp swap new # plain directories need <old>
	refused "$s0" || return 1
	sp swap new new
	refused "$s0" || return 1
	mkdir "$R/hermes-agent-old" && s0=$(snap "$R") # the name to keep them under is taken
	sp swap new old
	refused "$s0" || return 1
	newroot && mkplain "$R" old && mkpair "$R" new && mkpair "$R" third
	sp swap new old
	s0=$(snap "$R")
	sp swap third old # links already: no <old>
	refused "$s0" || return 1
	newroot && mkpair "$R" new
	s0=$(snap "$R")
	sp swap new old # nothing to keep
	refused "$s0" || return 1
}
c_bad_names() {
	newroot && mkplain "$R" old && mkpair "$R" new && mkpair "$R" 'a.b'
	s0=$(snap "$R")
	for nm in '../x' 'a b' '-n' '' 'a/b' 'new;x' 'a.b'; do
		sp swap "$nm" old
		refused "$s0" || { echo "new name '$nm'"; return 1; }
		sp swap new "$nm"
		refused "$s0" || { echo "old name '$nm'"; return 1; }
	done
}
c_usage() {
	newroot && mkplain "$R" old && mkpair "$R" new
	s0=$(snap "$R")
	for a in '' 'bogus' 'swap' 'swap a b c' 'rollback x' '-n'; do
		# shellcheck disable=SC2086 # the words are the test input
		sp $a
		refused "$s0" || { echo "args '$a'"; return 1; }
	done
	TEST_HERMES_LOCAL="$R/nope" sh "$S" swap new old >"$W/out" 2>&1 && RC=0 || RC=$?
	req [ "$RC" -eq 2 ] && req grep -q 'no such directory' "$W/out" || return 1
}
c_noop() {
	newroot && mkplain "$R" old && mkpair "$R" new
	sp swap new old
	s0=$(snap "$R")
	sp swap new
	req [ "$RC" -eq 0 ] && req [ "$(snap "$R")" = "$s0" ] && req grep -q 'nothing to do' "$W/out" || return 1
}
c_dry_run() {
	newroot && mkplain "$R" old && mkpair "$R" new
	s0=$(snap "$R")
	sp -n swap new old
	req [ "$RC" -eq 0 ] && req [ "$(snap "$R")" = "$s0" ] && req grep -q 'would rename' "$W/out" || return 1
	sp swap new old
	s0=$(snap "$R")
	sp -n rollback
	req [ "$RC" -eq 0 ] && req [ "$(snap "$R")" = "$s0" ] && req grep -q 'would repoint' "$W/out" || return 1
}
c_fresh_host() {
	newroot && mkpair "$R" new
	sp swap new
	req [ "$RC" -eq 0 ] && req link_is new && req [ ! -e "$R/pin-previous" ] || return 1
}
c_relative_root() {
	newroot && mkplain "$R" old && mkpair "$R" new
	(cd "$W" && TEST_HERMES_LOCAL="${R##*/}" sh "$S" swap new old >"$W/out" 2>&1) && RC=0 || RC=$?
	req [ "$RC" -eq 0 ] && req link_is new || return 1
}
c_stage_failure() { # a failure while staging leaves the plain directories untouched
	[ "$(id -u)" -ne 0 ] || return 0
	newroot && mkplain "$R" old && mkpair "$R" new
	s0=$(snap "$R")
	chmod 555 "$R"
	sp swap new old
	chmod 755 "$R"
	[ -w "$R" ] || return 0
	req [ "$RC" -ne 0 ] && req [ "$(snap "$R")" = "$s0" ] || return 1
}

CHECKS="first_swap link_swap rollback rollback_refused missing_target incomplete state_refused old_rules bad_names usage
noop dry_run fresh_host relative_root stage_failure"
for c in $CHECKS; do
	if out=$( (c_$c) 2>&1); then ok "$c"; else bad "$c: $out"; fi
done

# --- mutants: each must be caught by the named check --------------------------------------------------------------
mutant() { # mutant NAME SED-EXPRESSION CHECK
	sed "$2" "$REAL" >"$W/mutant.sh"
	if cmp -s "$REAL" "$W/mutant.sh"; then bad "mutant $1: the sed expression changed nothing"; return; fi
	S="$W/mutant.sh"
	if out=$( (c_$3) 2>&1); then bad "mutant $1 survived $3"; else ok "mutant $1 caught by $3"; fi
	S=$REAL
}
mutant no-target-check 's|^pair_ok "\$NEW" .*$|:|' missing_target
mutant no-pyvenv-check 's| && \[ -f "\$VE-\$1/pyvenv.cfg" \]||' incomplete
mutant plain-mv-onto-link 's|mv -T "\$AG\.\$T" "\$AG"|mv "$AG.$T" "$AG"|' link_swap
mutant no-record 's|mv -T "\$PREV\.\$T" "\$PREV"|:|' rollback
mutant no-old-required 's|\[ -n "\$OLD" \] \|\| die .*$|:|' old_rules
mutant no-mixed-check 's|^\*) die "\$AG and \$VE must be both.*$|*) ;;|' state_refused
mutant no-name-check 's|^is_short "\$NEW" .*$|:|' bad_names
mutant no-old-name-check 's|^\[ -z "\$OLD" \] .. is_short "\$OLD" .*$|:|' bad_names

echo "test_swap_pin: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
