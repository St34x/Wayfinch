#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Tests for tools/check-android-wrapper.sh: each case copies the real wrapper files into a scratch root and breaks one.
# The mutant cases remove one layer of the checker and require the matching case to go undetected there.
# Usage: sh tools/tests/test_check_android_wrapper.sh   (from anywhere inside the repo)
set -u

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
CHECK=$ROOT/tools/check-android-wrapper.sh
WORK=$(mktemp -d "${TMPDIR:-/tmp}/test-check-android-wrapper.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM
fail=0

fresh() { # fresh NAME -> a scratch root with the real wrapper files
	mkdir -p "$WORK/$1/android/gradle/wrapper"
	cp "$ROOT"/android/gradle/wrapper/gradle-wrapper.* "$WORK/$1/android/gradle/wrapper/"
	cp "$ROOT"/android/gradlew "$ROOT"/android/gradlew.bat "$WORK/$1/android/"
}

append() { # append NAME FILE LINE -> a fresh root whose FILE has one more line
	fresh "$1"
	printf '%s\n' "$3" >>"$WORK/$1/$2"
}

expect() { # expect NAME WANT-RC [PATTERN]
	out=$(sh "$CHECK" "$WORK/$1" 2>&1)
	rc=$?
	if [ "$rc" -ne "$2" ] || { [ $# -ge 3 ] && ! printf '%s\n' "$out" | grep -qF -- "$3"; }; then
		printf 'FAIL %s (rc %s, want %s): %s\n' "$1" "$rc" "$2" "$out"
		fail=1
	else
		printf 'ok   %s\n' "$1"
	fi
}

killed() { # killed NAME MUTANT-CHECKER CASE PATTERN: the mutant must miss CASE (rc 0 or no PATTERN in its output)
	out=$(sh "$2" "$WORK/$3" 2>&1)
	rc=$?
	if [ "$rc" -eq 0 ] || ! printf '%s\n' "$out" | grep -qF -- "$4"; then
		printf 'ok   %s (mutant misses %s)\n' "$1" "$3"
	else
		printf 'FAIL %s: mutant still reports %s: %s\n' "$1" "$3" "$out"
		fail=1
	fi
}

P=android/gradle/wrapper/gradle-wrapper.properties
J=android/gradle/wrapper/gradle-wrapper.jar
URL2='https\://attacker.example.com/distributions/gradle-9.8.0-bin.zip'

fresh good
expect good 0 OK
fresh url
sed 's/gradle-9.8.0-bin/gradle-9.8.1-bin/' "$ROOT/$P" >"$WORK/url/$P"
expect url 1 "$P"
fresh sum
sed 's/bafd5ce9/bafd5ce8/' "$ROOT/$P" >"$WORK/sum/$P"
expect sum 1 "$P"
fresh nosum
grep -v '^distributionSha256Sum' "$ROOT/$P" >"$WORK/nosum/$P"
expect nosum 1 "$P"
fresh novalidate
sed 's/^validateDistributionUrl=true/validateDistributionUrl=false/' "$ROOT/$P" >"$WORK/novalidate/$P"
expect novalidate 1 "$P"
fresh jar
{ cat "$ROOT/$J"; printf x; } >"$WORK/jar/$J"
expect jar 1 "$J"
fresh nojar
rm "$WORK/nojar/$J"
expect nojar 1 "$J"
fresh noprops
rm "$WORK/noprops/$P"
expect noprops 1 "$P"

# A second or differently spelled key: Java Properties keeps the last value and accepts ':' and blanks as separators.
append dupurl "$P" "distributionUrl=$URL2"
expect dupurl 1 "key 'distributionUrl' occurs 2 times"
append dupsum "$P" "distributionSha256Sum=0000000000000000000000000000000000000000000000000000000000000000"
expect dupsum 1 "key 'distributionSha256Sum' occurs 2 times"
append dupvalidate "$P" "validateDistributionUrl=false"
expect dupvalidate 1 "key 'validateDistributionUrl' occurs 2 times"
append colonurl "$P" "distributionUrl:$URL2"
expect colonurl 1 "key 'distributionUrl' occurs 2 times"
append spacedurl "$P" "distributionUrl = $URL2"
expect spacedurl 1 "key 'distributionUrl' occurs 2 times"
append blankurl "$P" "  distributionUrl $URL2"
expect blankurl 1 "key 'distributionUrl' occurs 2 times"

# Spellings the key check cannot see, and the other pinned files: only the SHA-256 pins catch them.
append escaped "$P" "distribution\\u0055rl=$URL2"
expect escaped 1 "$P: SHA-256"
append comment "$P" "# harmless-looking comment"
expect comment 1 "$P: SHA-256"
append gradlew android/gradlew "# appended"
expect gradlew 1 "android/gradlew: SHA-256"
append gradlewbat android/gradlew.bat "rem appended"
expect gradlewbat 1 "android/gradlew.bat: SHA-256"

# Planted mutants of the checker: without a layer, the cases above must go undetected (else they test nothing).
sed '/^check_once /d' "$CHECK" >"$WORK/mutant-nocount.sh"
killed mutant-nocount "$WORK/mutant-nocount.sh" dupurl "occurs 2 times"
killed mutant-nocount "$WORK/mutant-nocount.sh" colonurl "occurs 2 times"
sed '/^check_file_sum "/d' "$CHECK" >"$WORK/mutant-nohash.sh"
killed mutant-nohash "$WORK/mutant-nohash.sh" escaped "SHA-256"
killed mutant-nohash "$WORK/mutant-nohash.sh" gradlew "SHA-256"
# The layers are independent: with the hash layer removed, the key check alone still flags a duplicate key.
out=$(sh "$WORK/mutant-nohash.sh" "$WORK/dupurl" 2>&1)
rc=$?
if [ "$rc" -eq 1 ] && printf '%s\n' "$out" | grep -qF -- "occurs 2 times"; then
	echo "ok   key check alone flags dupurl"
else
	printf 'FAIL key check alone, dupurl (rc %s): %s\n' "$rc" "$out"
	fail=1
fi

exit "$fail"
