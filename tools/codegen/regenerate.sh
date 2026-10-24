#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# After an edit of protocol/envelope.schema.json or the message type table of protocol/README.md: regenerate every output
# derived from them (the Python types and the schema copy, then Envelope.kt). `make check-codegen` compares both.
# Usage: sh tools/codegen/regenerate.sh [PYTHON]   (default python3; the generators are stdlib-only, Python >= 3.11)
# Exit:  0 written; otherwise the code of the generator that failed (2: the schema uses something it cannot represent).
set -eu
py=${1:-python3}
dir=$(dirname "$0")
"$py" "$dir/gen_protocol.py"
"$py" "$dir/gen_kotlin.py"
