#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Gradle wrapper pin check (T1.17a; ENGINEERING.md section 8). POSIX sh + sha256sum; needs no JDK and no network.
# The pinned values are the official Gradle 9.8.0 ones (gradle.org release checksums). Two layers:
#   1. SHA-256 of the four wrapper files (gradle-wrapper.properties, gradle-wrapper.jar, gradlew, gradlew.bat).
#      The wrapper reads the properties file with java.util.Properties (last value wins; '=', ':' or blanks as
#      separator; backslash and unicode escapes), so a line-based check cannot see every spelling of an appended
#      override. Pinning the whole file can.
#   2. For a readable message: distributionUrl, distributionSha256Sum and validateDistributionUrl must each occur
#      exactly once, whatever the separator, with the pinned value.
# Upgrading Gradle = its own task: change the values here and the wrapper files in one commit.
#
# Usage: sh tools/check-android-wrapper.sh [ROOT]   (ROOT = repository root, default: the one of this script)
# Exit:  0 pinned, 1 a pin is missing or wrong (the file is named on stderr).
set -u

ROOT=${1:-$(cd "$(dirname "$0")/.." && pwd)}
DIR=$ROOT/android/gradle/wrapper
PROPS=$DIR/gradle-wrapper.properties
URL=distributionUrl=https://services.gradle.org/distributions/gradle-9.8.0-bin.zip
SUM=distributionSha256Sum=bafd5ce9cfaea0fbccfdc8439a1ac42fbd4cd9c89dc9a988228d8a2639a58e6c
PROPS_SUM=e81b90975868be2513ca00d6a5a01303a406a5cb01a306210b887e983e4b17ae
JAR_SUM=238e777fcddd7e34f9708186085def2abd6e08e658505b38718d79d74c21abd5
GRADLEW_SUM=a5a5c199ba02189ae8c46a334223371a20599d9c298ef65e7540ede4a3f72d59
GRADLEW_BAT_SUM=f17917f8dbe61182b149273ee78090fec1197442c3f38276af907bfa5190db16

bad=0
complain() { echo "check-android-wrapper: $*" >&2; bad=1; }

check_file_sum() { # check_file_sum FILE EXPECTED-SHA256
	got=$(sha256sum "$1" 2>/dev/null | cut -d' ' -f1)
	[ "$got" = "$2" ] || complain "$1: SHA-256 is '${got:-unreadable}', expected $2"
}

check_once() { # check_once KEY WANTED-LINE; reads the properties text from $props
	n=$(printf '%s\n' "$props" | grep -cE "^[[:space:]]*$1([^A-Za-z0-9_.-]|\$)")
	[ "$n" -eq 1 ] || complain "$PROPS: key '$1' occurs $n times, expected exactly once"
	printf '%s\n' "$props" | grep -qxF -- "$2" || complain "$PROPS: missing line '$2'"
}

props=$(sed 's/\\:/:/g' "$PROPS" 2>/dev/null)
check_once distributionUrl "$URL"
check_once distributionSha256Sum "$SUM"
check_once validateDistributionUrl validateDistributionUrl=true
check_file_sum "$PROPS" "$PROPS_SUM"
check_file_sum "$DIR/gradle-wrapper.jar" "$JAR_SUM"
check_file_sum "$ROOT/android/gradlew" "$GRADLEW_SUM"
check_file_sum "$ROOT/android/gradlew.bat" "$GRADLEW_BAT_SUM"
[ "$bad" -ne 0 ] || echo "check-android-wrapper: OK (Gradle 9.8.0 wrapper pinned)"
exit "$bad"
