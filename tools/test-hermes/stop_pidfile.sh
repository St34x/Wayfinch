#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Stop a process the test harness started, by pid file -- but only if it is still ours.
# Usage: sh tools/test-hermes/stop_pidfile.sh PIDFILE PATTERN
# The pid file is always removed. The pid is killed (SIGTERM) only when its command line (NUL-separated argv
# joined with spaces) contains PATTERN, e.g. "mock_llm.py" or "hermes gateway run"; a stale pid reused by an
# unrelated process is left alone. Exit 0 in every case except a failed kill of a matching process.
set -u
[ $# -eq 2 ] && [ -n "$2" ] || { echo "usage: stop_pidfile.sh PIDFILE PATTERN (non-empty)" >&2; exit 2; }
f=$1
pat=$2
if [ ! -e "$f" ]; then
	echo "stop_pidfile: no $f, nothing to stop"
	exit 0
fi
pid=$(head -c 32 "$f" 2>/dev/null | tr -d ' \n')
rm -f "$f"
case "$pid" in '' | *[!0-9]* | 0*)
	echo "stop_pidfile: $f held no valid pid, removed"
	exit 0
	;;
esac
if [ -r "/proc/$pid/cmdline" ]; then
	cmd=$(tr '\000' ' ' 2>/dev/null <"/proc/$pid/cmdline")
else
	cmd=$(ps -o args= -p "$pid" 2>/dev/null) || cmd=
fi
case "$cmd" in
*"$pat"*)
	kill "$pid" && echo "stop_pidfile: stopped $pid ($pat)"
	;;
*)
	echo "stop_pidfile: pid $pid is not $pat (gone or reused), not killed; $f removed"
	;;
esac
