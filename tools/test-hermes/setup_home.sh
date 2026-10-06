#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Build the multiplexed test Hermes home $TEST_HERMES_LOCAL/hh (default profile + neutral test profiles)
# for the pinned hermes-agent clone. Clean env (env -i) for every hermes call. Safe to re-run.
# Never touches a live install: everything lives under TEST_HERMES_LOCAL.
#   TEST_HERMES_LOCAL  scratch root (clone, venv, home)       default <repo>/.local  (no '#', '&' or '\')
#   TEST_HERMES_VENV   venv with the pinned hermes            default $TEST_HERMES_LOCAL/venv314
#   HERMES_REF         expected commit of $TEST_HERMES_LOCAL/hermes-agent (checked when the clone exists)
#   TEST_LLM_PORT      loopback port of the mock LLM          default 18080
#   TEST_GATEWAY_PORT  loopback port of the glasses plugin    default 18700
#   TEST_PROFILES      named test profiles                    default "alpha beta"; the first one is the
#                      default lane of a glasses frame without "lane"
# Profiles this script creates carry a $MARK file; a marked profile that is no longer in TEST_PROFILES is
# deleted on the next run. Unmarked profiles (made by hand or by older runs) are never touched.
set -eu
PROJ=$(cd "$(dirname "$0")/../.." && pwd)
S="$PROJ/tools/test-hermes"
P="${TEST_HERMES_LOCAL:-$PROJ/.local}"
V="${TEST_HERMES_VENV:-$P/venv314}"
REF="${HERMES_REF:-a4648c5f58}"
LLM_PORT="${TEST_LLM_PORT:-18080}"
GW_PORT="${TEST_GATEWAY_PORT:-18700}"
PROFILES="${TEST_PROFILES:-alpha beta}"
H="$P/hh"
MARK=.test-harness-profile
die() { echo "setup_home: $*" >&2; exit 2; }
case "$LLM_PORT:$GW_PORT" in *[!0-9:]* | :* | *:) die "ports must be numeric" ;; esac
case "$P" in *[#\&\\]*) die "TEST_HERMES_LOCAL must not contain '#', '&' or '\\': $P" ;; esac
[ -x "$V/bin/hermes" ] || die "no hermes in $V/bin (build the pinned venv first)"
if [ -e "$P/hermes-agent/.git" ]; then
	head=$(git -C "$P/hermes-agent" rev-parse HEAD)
	case "$head" in "$REF"*) ;; *) die "hermes-agent clone is at $head, expected HERMES_REF=$REF" ;; esac
fi
DEFAULT_LANE=
for prof in $PROFILES; do DEFAULT_LANE=$prof && break; done
HX() { env -i HOME="$P/fakehome" PATH="$V/bin:/usr/bin:/bin" HERMES_HOME="$H" TERM=dumb "$@"; }
render() {
	sed -e "s#@P@#$P#g" -e "s#@LLM_PORT@#$LLM_PORT#g" -e "s#@DEFAULT_LANE@#$DEFAULT_LANE#g" \
		-e "s#^\( *port: \)18700\$#\1$GW_PORT#" "$1"
}
routes() {
	for prof in $PROFILES; do
		printf '    - name: glasses-%s\n      platform: glasses\n      chat_id: "glasses:%s"\n      profile: %s\n' \
			"$prof" "$prof" "$prof"
	done
	printf '    - name: glasses-ghost\n      platform: glasses\n      chat_id: "glasses:ghost"\n      profile: ghost\n'
}
listed() { for q in $PROFILES; do [ "$q" = "$1" ] && return 0; done; return 1; }
mkdir -p "$H/plugins" "$P/fakehome"
[ -s "$P/glasses-token" ] || (umask 077; head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$P/glasses-token")
routes >"$H/.routes.tmp"
render "$S/config-default.yaml" | sed -e "/^    # @ROUTES@\$/r $H/.routes.tmp" -e '/^    # @ROUTES@$/d' >"$H/config.yaml"
rm -f "$H/.routes.tmp"
printf 'PROFILE-MARKER=default\nYou are the default test profile.\n' >"$H/SOUL.md"
printf 'GLASSES_ALLOWED_USERS=operator\n' >"$H/.env"
ln -sfn "$S/glasses_min" "$H/plugins/glasses"
for prof in $PROFILES; do
	[ -d "$H/profiles/$prof" ] || HX hermes profile create "$prof" --no-alias --no-skills </dev/null
	render "$S/config-profile.yaml" >"$H/profiles/$prof/config.yaml"
	printf 'PROFILE-MARKER=%s\nYou are the %s test profile.\n' "$prof" "$prof" >"$H/profiles/$prof/SOUL.md"
	: >"$H/profiles/$prof/$MARK"
done
for d in "$H"/profiles/*; do
	[ -f "$d/$MARK" ] || continue
	listed "${d##*/}" || { rm -rf "$d" && echo "setup_home: pruned stale test profile ${d##*/}"; }
done
HX hermes profile list </dev/null || true
