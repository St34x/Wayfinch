#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Tamper tests of the `make venv` install (pip --require-hashes --only-binary :all:), offline.
# Runs a copy of the Makefile in a temp dir against a local fixture package `fxpkg` (one wheel and
# one sdist with an in-tree build backend), served as a file:// simple index whose links carry
# #sha256= fragments like PyPI's, so pip pre-filters files by the pinned hashes as it does online.
# No network.
# Cases print "ok", "FAIL" or "note" (pip behaviour outside this repo's control; does not fail).
#
# Usage: sh tools/tests/test_venv_hashes.sh   (from the repo root; MAKE=..., PYTHON=... to override)
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
MAKE=${MAKE:-make}
PYTHON=${PYTHON:-python3}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

PASS=0
FAIL=0
NOTE=0
ok() { PASS=$((PASS + 1)); echo "ok   - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "FAIL - $1"; sed 's/^/       | /' "$WORK/out"; return 0; }
note() { NOTE=$((NOTE + 1)); echo "note - $1"; }

FX="$WORK/fx"
mkdir -p "$FX"
# Fixture: fxpkg 1.0 as a wheel (fxpkg.ORIGIN == "wheel") and as an sdist whose in-tree backend
# builds a wheel with ORIGIN == "sdist" and needs no build dependencies (works offline).
"$PYTHON" - "$FX" <<'EOF'
import base64, hashlib, io, sys, tarfile, zipfile
fx = sys.argv[1]
META = "Metadata-Version: 2.1\nName: fxpkg\nVersion: 1.0\n"
BACKEND = '''import base64, hashlib, os, zipfile
def _rec(name, data):
    d = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return "%s,sha256=%s,%d" % (name, d, len(data))
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    files = {"fxpkg/__init__.py": b'ORIGIN = "sdist"\\n',
             "fxpkg-1.0.dist-info/METADATA": open("PKG-INFO", "rb").read(),
             "fxpkg-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}
    rec = "\\n".join(_rec(n, d) for n, d in files.items()) + "\\nfxpkg-1.0.dist-info/RECORD,,\\n"
    files["fxpkg-1.0.dist-info/RECORD"] = rec.encode()
    name = "fxpkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return name
'''
def rec(name, data):
    d = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return "%s,sha256=%s,%d" % (name, d, len(data))
files = {"fxpkg/__init__.py": b'ORIGIN = "wheel"\n',
         "fxpkg-1.0.dist-info/METADATA": META.encode(),
         "fxpkg-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
files["fxpkg-1.0.dist-info/RECORD"] = ("\n".join(rec(n, d) for n, d in files.items())
                                       + "\nfxpkg-1.0.dist-info/RECORD,,\n").encode()
with zipfile.ZipFile(fx + "/fxpkg-1.0-py3-none-any.whl", "w") as z:
    for n, d in files.items():
        z.writestr(n, d)
sdist = {"PKG-INFO": META.encode(), "backend.py": BACKEND.encode(),
         "fxpkg/__init__.py": b'ORIGIN = "sdist"\n',
         "pyproject.toml": b'[build-system]\nrequires = []\nbuild-backend = "backend"\nbackend-path = ["."]\n'}
with tarfile.open(fx + "/fxpkg-1.0.tar.gz", "w:gz") as t:
    for n, d in sdist.items():
        info = tarfile.TarInfo("fxpkg-1.0/" + n)
        info.size = len(d)
        t.addfile(info, io.BytesIO(d))
EOF
WHL=$("$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$FX/fxpkg-1.0-py3-none-any.whl")
SDIST=$("$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$FX/fxpkg-1.0.tar.gz")
BADWHL=$(printf '%s' "$WHL" | tr '0123456789abcdef' '123456789abcdef0')
mkdir -p "$WORK/simple/fxpkg"
printf '<!DOCTYPE html><html><body>\n<a href="%s#sha256=%s">%s</a>\n<a href="%s#sha256=%s">%s</a>\n</body></html>\n' \
	"file://$FX/fxpkg-1.0-py3-none-any.whl" "$WHL" fxpkg-1.0-py3-none-any.whl \
	"file://$FX/fxpkg-1.0.tar.gz" "$SDIST" fxpkg-1.0.tar.gz >"$WORK/simple/fxpkg/index.html"

S="$WORK/tree"
fresh() { # fresh <requirements line>: Makefile copy, empty mk/, fixture requirements-dev.txt
	rm -rf "$S"
	mkdir -p "$S/mk"
	cp "$ROOT/Makefile" "$S/Makefile"
	printf '%s\n' "$1" >"$S/requirements-dev.txt"
}
run_venv() { # run_venv <venv name>: make venv offline against the fixture index
	set +e
	PIP_INDEX_URL="file://$WORK/simple/" PIP_NO_CACHE_DIR=1 PIP_CONFIG_FILE=/dev/null \
		$MAKE --no-print-directory -C "$S" venv VENV="$WORK/$1" PYTHON="$PYTHON" >"$WORK/out" 2>&1
	RC=$?
	set -e
}
origin() { "$WORK/$1/bin/python" -c 'import fxpkg; print(fxpkg.ORIGIN)' 2>/dev/null || echo none; }

# V1. The recipe asks pip for hashes and binaries only.
fresh "fxpkg==1.0"
$MAKE --no-print-directory -C "$S" -n venv >"$WORK/out" 2>&1 || true
if grep -q -- '--require-hashes' "$WORK/out" && grep -q -- '--only-binary :all:' "$WORK/out"; then
	ok "make venv runs pip with --require-hashes --only-binary :all:"
else bad "make venv recipe flags"; fi

# V2. Intact hashes: the wheel is installed.
fresh "fxpkg==1.0 --hash=sha256:$WHL --hash=sha256:$SDIST"
run_venv v2
if [ "$RC" = 0 ] && [ "$(origin v2)" = wheel ]; then ok "intact hashes: wheel installed"; else bad "intact hashes (rc $RC, origin $(origin v2))"; fi

# V3. Wheel hash tampered, sdist hash intact: refused, no sdist build, nothing installed.
fresh "fxpkg==1.0 --hash=sha256:$BADWHL --hash=sha256:$SDIST"
run_venv v3
if [ "$RC" != 0 ] && grep -q 'DO NOT MATCH THE HASHES' "$WORK/out" && ! grep -q 'Building wheel' "$WORK/out" &&
	[ "$(origin v3)" = none ]; then
	ok "tampered wheel hash: refused, no sdist fallback (rc $RC)"
else bad "tampered wheel hash (rc $RC, origin $(origin v3))"; fi

# V4. A pin without a hash: refused.
fresh "fxpkg==1.0"
run_venv v4
if [ "$RC" != 0 ] && grep -q 'Hashes are required' "$WORK/out"; then ok "missing hash: refused"; else bad "missing hash (rc $RC)"; fi

# V5. Control (shows V3 is not vacuous): without --only-binary the same tamper builds the sdist.
fresh "fxpkg==1.0 --hash=sha256:$BADWHL --hash=sha256:$SDIST"
sed 's/ --only-binary :all://' "$ROOT/Makefile" >"$S/Makefile"
run_venv v5
if [ "$RC" = 0 ] && [ "$(origin v5)" = sdist ]; then
	ok "control: without --only-binary pip falls back to the sdist"
else note "control: pip did not fall back to the sdist (rc $RC, origin $(origin v5))"; fi

echo "test_venv_hashes: $PASS passed, $FAIL failed, $NOTE note(s)"
[ "$FAIL" = 0 ]
