#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Tests of tools/test-hermes (setup_home.sh with a fake `hermes`, stop_pidfile.sh with throwaway processes).
# No real Hermes, no network, no ports. Usage: sh tools/test-hermes/test_harness.sh (repo root; PY = python with
# PyYAML, default .local/venv-dev/bin/python). Part of make test via mk/test-hermes.mk (test-hermes-harness).
set -u
unset TEST_HERMES_LOCAL TEST_HERMES_VENV HERMES_REF TEST_LLM_PORT TEST_GATEWAY_PORT TEST_PROFILES
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
W=$(mktemp -d)
trap 'rm -rf "$W"' EXIT INT TERM
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); echo "ok   $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL $1"; }
mkdir -p "$W/venv/bin"
printf '#!/bin/sh\necho "fake hermes $*" >>"$HERMES_HOME/calls"\n[ "$1 $2" = "profile create" ] && mkdir -p "$HERMES_HOME/profiles/$3"\nexit 0\n' \
	>"$W/venv/bin/hermes"
chmod +x "$W/venv/bin/hermes"
run() { env TEST_HERMES_LOCAL="$W/l" TEST_HERMES_VENV="$W/venv" "$@" sh "$ROOT/tools/test-hermes/setup_home.sh" >"$W/out" 2>&1; }

run TEST_LLM_PORT=28080 TEST_GATEWAY_PORT=28700
RC=$?
C="$W/l/hh/config.yaml"
[ "$RC" -eq 0 ] && ok "setup succeeds with a fake hermes" || bad "setup succeeds (rc=$RC: $(cat "$W/out"))"
grep -q 'api: http://127.0.0.1:28080/v1' "$C" && ok "LLM port rendered" || bad "LLM port rendered"
grep -q '^        port: 28700$' "$C" && ok "gateway port rendered" || bad "gateway port rendered"
grep -q "token_file: \"$W/l/glasses-token\"" "$C" && ok "scratch root rendered" || bad "scratch root rendered"
grep -q 'chat_id: "glasses:alpha"' "$C" && grep -q 'chat_id: "glasses:beta"' "$C" && grep -q 'profile: ghost' "$C" &&
	ok "routes for default profiles + missing one" || bad "routes rendered"
grep -v '^#' "$C" | grep -q '@ROUTES@\|@P@\|@LLM_PORT@' && bad "no placeholder left" || ok "no placeholder left"
"${PY:-$ROOT/.local/venv-dev/bin/python}" - "$C" <<'EOF' && ok "rendered config is valid YAML" || bad "rendered config is valid YAML"
import sys, yaml
c = yaml.safe_load(open(sys.argv[1]))
assert [r["profile"] for r in c["gateway"]["profile_routes"]] == ["alpha", "beta", "ghost"]
assert c["gateway"]["platforms"]["glasses"]["extra"]["port"] == 28700
assert c["gateway"]["platforms"]["glasses"]["extra"]["default_lane"] == "alpha"
EOF
grep -q 'api: http://127.0.0.1:28080/v1' "$W/l/hh/profiles/alpha/config.yaml" && ok "profile config rendered" ||
	bad "profile config rendered"
[ "$(readlink "$W/l/hh/plugins/glasses")" = "$ROOT/tools/test-hermes/glasses_min" ] && ok "plugin linked" ||
	bad "plugin linked"
[ -s "$W/l/glasses-token" ] && ok "token created" || bad "token created"

run TEST_PROFILES="one"
grep -q 'chat_id: "glasses:one"' "$C" && ! grep -q 'glasses:alpha' "$C" && grep -q '^        port: 18700$' "$C" &&
	ok "TEST_PROFILES and default ports" || bad "TEST_PROFILES and default ports"
grep -q '^        default_lane: "one"$' "$C" && ok "default lane = first TEST_PROFILES entry" ||
	bad "default lane = first TEST_PROFILES entry"
[ ! -e "$W/l/hh/profiles/alpha" ] && [ ! -e "$W/l/hh/profiles/beta" ] && [ -f "$W/l/hh/profiles/one/config.yaml" ] &&
	grep -q 'pruned stale test profile alpha' "$W/out" && ok "stale harness profiles pruned" ||
	bad "stale harness profiles pruned ($(cat "$W/out"))"
mkdir -p "$W/l/hh/profiles/handmade" && echo keep >"$W/l/hh/profiles/handmade/SOUL.md"
run TEST_PROFILES="two"
[ -f "$W/l/hh/profiles/handmade/SOUL.md" ] && [ ! -e "$W/l/hh/profiles/one" ] &&
	ok "unmarked profile kept, marked one pruned" || bad "unmarked profile kept, marked one pruned"

for bad_path in 'a#b' 'a&b' 'a\b'; do
	env TEST_HERMES_LOCAL="$W/$bad_path" TEST_HERMES_VENV="$W/venv" sh "$ROOT/tools/test-hermes/setup_home.sh" \
		>"$W/out" 2>&1
	[ $? -eq 2 ] && grep -q 'must not contain' "$W/out" && [ ! -e "$W/$bad_path" ] &&
		ok "TEST_HERMES_LOCAL with $bad_path refused" || bad "TEST_HERMES_LOCAL with $bad_path refused ($(cat "$W/out"))"
done

run TEST_GATEWAY_PORT=x1
[ $? -eq 2 ] && grep -q 'numeric' "$W/out" && ok "non-numeric port refused" || bad "non-numeric port refused"

mkdir -p "$W/l/hermes-agent" && git -C "$W/l/hermes-agent" init -q &&
	git -C "$W/l/hermes-agent" -c user.email=t@example.com -c user.name=t commit -q --allow-empty -m x
run HERMES_REF=0000000
[ $? -eq 2 ] && grep -q 'expected HERMES_REF=0000000' "$W/out" && ok "wrong clone ref refused" ||
	bad "wrong clone ref refused ($(cat "$W/out"))"
run HERMES_REF="$(git -C "$W/l/hermes-agent" rev-parse --short HEAD)"
[ $? -eq 0 ] && ok "matching clone ref accepted" || bad "matching clone ref accepted ($(cat "$W/out"))"

env TEST_HERMES_LOCAL="$W/l" TEST_HERMES_VENV="$W/none" sh "$ROOT/tools/test-hermes/setup_home.sh" >"$W/out" 2>&1
[ $? -eq 2 ] && grep -q 'no hermes' "$W/out" && ok "missing venv refused" || bad "missing venv refused"

# stop_pidfile.sh: kills only a process whose argv still matches; the pid file always goes.
STOP="$ROOT/tools/test-hermes/stop_pidfile.sh"
argv_has() { i=0; while [ $i -lt 50 ]; do tr '\000' ' ' <"/proc/$1/cmdline" 2>/dev/null | grep -q "$2" && return 0;
	sleep 0.1; i=$((i + 1)); done; return 1; }
sleep 30 &
OTHER=$!
echo "$OTHER" >"$W/stale.pid"
sh "$STOP" "$W/stale.pid" 'mock_llm.py' >"$W/out" 2>&1
RC=$?
kill -0 "$OTHER" 2>/dev/null && [ "$RC" -eq 0 ] && [ ! -e "$W/stale.pid" ] && grep -q 'not killed' "$W/out" &&
	ok "stale pid of an unrelated process: survives, pid file removed" || bad "stale pid ($RC: $(cat "$W/out"))"
kill "$OTHER" 2>/dev/null
wait "$OTHER" 2>/dev/null
printf 'import time\ntime.sleep(5)\n' >"$W/mock_llm.py"
"${PY:-$ROOT/.local/venv-dev/bin/python}" "$W/mock_llm.py" --port 1 &
OURS=$!
argv_has "$OURS" 'mock_llm.py --port 1' || bad "fake mock argv never appeared"
echo "$OURS" >"$W/ours.pid"
sh "$STOP" "$W/ours.pid" 'mock_llm.py --port 1' >"$W/out" 2>&1
RC=$?
wait "$OURS" 2>/dev/null
[ $? -eq 143 ] && [ "$RC" -eq 0 ] && [ ! -e "$W/ours.pid" ] && ok "matching pid killed (SIGTERM), pid file removed" ||
	bad "matching pid killed ($RC: $(cat "$W/out"))"
printf 'abc\n' >"$W/junk.pid"
sh "$STOP" "$W/junk.pid" x >"$W/out" 2>&1 && [ ! -e "$W/junk.pid" ] && ok "junk pid file removed, nothing killed" ||
	bad "junk pid file"
sh "$STOP" "$W/none.pid" x >"$W/out" 2>&1 && grep -q 'nothing to stop' "$W/out" && ok "missing pid file is fine" ||
	bad "missing pid file"
sh "$STOP" "$W/none.pid" '' >"$W/out" 2>&1
[ $? -eq 2 ] && ok "empty pattern refused" || bad "empty pattern refused"
# Never signal -1 / 0 / a zero-padded pid (kill -1 = every process of the user), even if argv matches.
sleep 30 &
LIVE=$!
for raw in '-1' '0' "00$LIVE"; do
	printf '%s\n' "$raw" >"$W/odd.pid"
	sh "$STOP" "$W/odd.pid" 'sleep 30' >"$W/out" 2>&1
	RC=$?
	kill -0 "$LIVE" 2>/dev/null && [ "$RC" -eq 0 ] && [ ! -e "$W/odd.pid" ] && grep -q 'no valid pid' "$W/out" &&
		ok "pid file '$raw' refused, nothing killed" || bad "pid file '$raw' ($RC: $(cat "$W/out"))"
done
kill "$LIVE" 2>/dev/null
wait "$LIVE" 2>/dev/null
echo "$LIVE" >"$W/gone.pid"
sh "$STOP" "$W/gone.pid" 'sleep 30' >"$W/out" 2>&1 && [ ! -e "$W/gone.pid" ] && grep -q 'not killed' "$W/out" &&
	ok "pid of an exited process: nothing killed, pid file removed" || bad "exited pid ($(cat "$W/out"))"

echo "test_harness: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
