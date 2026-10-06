#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Run the TEST gateway for the multiplexed home $TEST_HERMES_LOCAL/hh in the foreground. Clean env.
# No API server (it stays disabled); the glasses plugin binds 127.0.0.1:$TEST_GATEWAY_PORT (see setup_home.sh).
set -eu
PROJ=$(cd "$(dirname "$0")/../.." && pwd)
P="${TEST_HERMES_LOCAL:-$PROJ/.local}"
V="${TEST_HERMES_VENV:-$P/venv314}"
# exec keeps this pid: test-hermes-down stops it via stop_pidfile.sh only while its argv is "hermes gateway run".
echo $$ >"$P/gateway.pid"
exec env -i HOME="$P/fakehome" PATH="$V/bin:/usr/bin:/bin" HERMES_HOME="$P/hh" TERM=dumb \
	hermes gateway run -v
