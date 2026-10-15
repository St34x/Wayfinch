#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Secret scan with a pinned, checksum-verified gitleaks.
#
# Usage: tools/secret-scan.sh [--all|--tree|--history] [DIR]
#        tools/secret-scan.sh --version
#   --tree     scan the working tree: tracked files plus untracked files git does not ignore
#              (so gitignored local state such as .local/ or deploy/local/ is never read)
#   --history  scan every commit reachable from HEAD (deleted secrets stay in history)
#   --all      both (default)
# DIR, when given, must be the top of a git work tree (a subdirectory is refused with exit 2, so a
# gitignored subdir is never reported clean). Without DIR the work tree containing the current directory
# is scanned from its top.
# Config: <repo top>/.gitleaks.toml. Known false positives: add the "Fingerprint:" line of the report to
# <repo top>/.gitleaksignore, or (private fingerprints) <repo top>/.team/gitleaksignore; both are read.
# Each run logs "secret-scan: N ignore entries from <files>" to stderr (a count, never the entries).
#
# Env: GITLEAKS_CACHE_DIR  where the verified release archive is cached
#                          (default: <repo top>/.local/gitleaks, gitignored)
#
# Exit: 0 no leaks, 1 leaks found (file, line, rule shown; values redacted), 2 error (not a git work
# tree, file list/copy failed, unreadable file, download/checksum failure, gitleaks itself failed).
# The scan fails closed: it never reports "no leaks" for a tree it could not list or copy completely.
#
# The archive is downloaded once, its SHA-256 is checked against the value pinned below on every
# run, and the binary is extracted to a temp dir for each run. A mismatch is a hard failure.
set -eu

GITLEAKS_VERSION=8.30.1
# SHA-256 of the official release archives, copied from
# https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_checksums.txt
# (cross-checked against the asset digests of the GitHub release API for tag v8.30.1).
pinned_sha256() {
	case "$1" in
	linux_x64) echo 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
	linux_arm64) echo e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
	darwin_x64) echo dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 ;;
	darwin_arm64) echo b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
	*) return 1 ;;
	esac
}

die() {
	echo "secret-scan: $*" >&2
	exit 2
}

platform() {
	case "$(uname -s)" in
	Linux) os=linux ;;
	Darwin) os=darwin ;;
	*) die "unsupported OS $(uname -s)" ;;
	esac
	case "$(uname -m)" in
	x86_64 | amd64) arch=x64 ;;
	aarch64 | arm64) arch=arm64 ;;
	*) die "unsupported CPU $(uname -m)" ;;
	esac
	echo "${os}_${arch}"
}

sha256_of() {
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" | cut -d' ' -f1
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 "$1" | cut -d' ' -f1
	else
		die "need sha256sum or shasum"
	fi
}

download() { # download <url> <dest>
	if command -v curl >/dev/null 2>&1; then
		curl -fsSL --proto '=https' --tlsv1.2 -o "$2" "$1"
	elif command -v wget >/dev/null 2>&1; then
		wget -q --https-only -O "$2" "$1"
	else
		die "need curl or wget to download gitleaks"
	fi
}

MODE=all
case "${1:-}" in
--all | --tree | --history)
	MODE=${1#--}
	shift
	;;
--version)
	MODE=version
	shift
	;;
-h | --help)
	sed -n '4,23p' "$0"
	exit 0
	;;
-*) die "unknown option $1" ;;
esac
[ $# -le 1 ] || die "too many arguments"
ARG=$(cd "${1:-.}" && pwd) || die "no such directory: ${1:-.}"
if [ "$(git -C "$ARG" rev-parse --is-inside-work-tree 2>/dev/null)" = true ]; then
	REPO=$(git -C "$ARG" rev-parse --show-toplevel) || die "cannot find the top of the work tree of $ARG"
	# An explicit DIR must be the top itself: mapping a (possibly gitignored) subdir to the enclosing top
	# would scan the parent and report the unread DIR as clean.
	if [ $# -eq 1 ] && [ "$MODE" != version ]; then
		prefix=$(git -C "$ARG" rev-parse --show-prefix) || die "cannot locate $ARG in its work tree"
		[ -z "$prefix" ] || die "$1 is not the top of a git work tree (top: $REPO) - pass the top or run without DIR"
	fi
elif [ "$MODE" = version ]; then
	REPO=$ARG
else
	# Fail closed: everything below needs git to describe the tree.
	die "$ARG is not a git work tree (git rev-parse failed) - refusing to report an unscanned tree as clean"
fi

PLAT=$(platform)
WANT=$(pinned_sha256 "$PLAT") || die "no pinned checksum for $PLAT"
CACHE=${GITLEAKS_CACHE_DIR:-$REPO/.local/gitleaks}
ARCHIVE="$CACHE/gitleaks_${GITLEAKS_VERSION}_${PLAT}.tar.gz"

TMP=$(mktemp -d) || die "mktemp failed"
DL=""
trap 'rm -rf "$TMP" || :; [ -z "$DL" ] || rm -f "$DL" || :' EXIT # a failed cleanup must not change the exit code (set -e)
trap 'exit 130' INT
trap 'exit 143' TERM

if [ ! -f "$ARCHIVE" ]; then
	mkdir -p "$CACHE" || die "cannot create cache dir $CACHE"
	url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/$(basename "$ARCHIVE")"
	echo "secret-scan: downloading $url" >&2
	# Download next to the final name, so the mv below is an atomic rename (parallel first runs are safe).
	DL="$CACHE/.dl.$$.tar.gz"
	download "$url" "$DL" || die "download failed: $url"
	got=$(sha256_of "$DL")
	[ "$got" = "$WANT" ] || die "checksum mismatch for downloaded $(basename "$ARCHIVE"): got $got, want $WANT"
	mv -f "$DL" "$ARCHIVE" || die "cannot move the download into $CACHE"
	DL=""
fi
got=$(sha256_of "$ARCHIVE")
[ "$got" = "$WANT" ] || die "checksum mismatch for cached $ARCHIVE: got $got, want $WANT (delete it to re-download)"
tar -xzf "$ARCHIVE" -C "$TMP" gitleaks || die "cannot extract $ARCHIVE"
GL="$TMP/gitleaks"

if [ "$MODE" = version ]; then
	"$GL" version || die "gitleaks version failed"
	exit 0
fi

# gitleaks exits LEAK_RC on findings and non-zero otherwise on its own errors (bad config, I/O, ...);
# the two are reported differently so a tool failure is never mistaken for (or hidden as) a leak.
LEAK_RC=3
set -- --no-banner --redact --verbose --exit-code "$LEAK_RC"
[ -f "$REPO/.gitleaks.toml" ] && set -- "$@" --config "$REPO/.gitleaks.toml"
# Known false positives: the public .gitleaksignore plus the private one under the team folder (a fingerprint
# of a finding in a private file names that file, so it cannot live in the exported tree). Merged into one file.
IGN="$TMP/gitleaksignore"
printf '' >"$IGN" || die "cannot create $IGN"
USED=""
for f in "$REPO/.gitleaksignore" "$REPO/.team/gitleaksignore"; do
	if [ -f "$f" ]; then
		{ cat "$f" && echo; } >>"$IGN" || die "cannot read $f"
		USED="$USED ${f#"$REPO"/}"
	fi
done
if [ -n "$USED" ]; then
	# Say that fingerprints are applied and how many (a count, never the entries), so a growing ignore set
	# shows up in the log of every run.
	COUNT=$(awk '$0 !~ /^[ 	\r]*$/ && $0 !~ /^[ 	]*#/ { n++ } END { print n + 0 }' "$IGN") ||
		die "cannot count the entries of the ignore files"
	echo "secret-scan: $COUNT ignore entries from$USED" >&2
fi
[ -s "$IGN" ] && set -- "$@" --gitleaks-ignore-path "$IGN"

rc=0
gl() { # gl <what> <gitleaks args...>: 0 clean, sets rc=1 on leaks, dies on any other failure
	what=$1
	shift
	set +e
	"$GL" "$@"
	n=$?
	set -e
	case "$n" in
	0) ;;
	"$LEAK_RC") rc=1 ;;
	*) die "gitleaks failed on the $what (exit $n) - this is a tool error, not a leak; scan incomplete" ;;
	esac
}

if [ "$MODE" = all ] || [ "$MODE" = tree ]; then
	# Copy exactly what git would see (tracked + untracked-but-not-ignored, existing files only) so that
	# gitignored local state is never read. Every step is checked: a failed listing or copy is an error.
	git -C "$REPO" ls-files -z --cached --others --exclude-standard >"$TMP/list" ||
		die "cannot list the working tree of $REPO (git ls-files failed)"
	(cd "$REPO" && xargs -0 sh -c 'for f; do if [ -f "$f" ] || [ -L "$f" ]; then printf "%s\0" "$f"; fi; done' sh \
		<"$TMP/list" >"$TMP/files") || die "cannot filter the file list of $REPO"
	[ -s "$TMP/files" ] || die "no files to scan in $REPO - refusing to report an empty scan as clean"
	(cd "$REPO" && tar --null -T "$TMP/files" -cf "$TMP/tree.tar") ||
		die "cannot copy the working tree of $REPO (unreadable file?) - scan not run"
	mkdir "$TMP/tree" || die "cannot create $TMP/tree"
	tar -xf "$TMP/tree.tar" -C "$TMP/tree" || die "cannot unpack the working tree copy"
	rm -f "$TMP/tree.tar"
	echo "secret-scan: working tree of $REPO" >&2
	# Scan from inside the copy so paths and fingerprints are repo-relative (.gitleaksignore matches).
	cd "$TMP/tree"
	gl "working tree" dir "$@" .
	cd "$REPO"
fi
if [ "$MODE" = all ] || [ "$MODE" = history ]; then
	if git -C "$REPO" rev-parse --verify -q HEAD >/dev/null; then
		echo "secret-scan: history of HEAD in $REPO" >&2
		gl history git "$@" --log-opts=HEAD "$REPO"
	else
		echo "secret-scan: no commits yet, history skipped" >&2
	fi
fi

if [ "$rc" -ne 0 ]; then
	echo "secret-scan: FAILED - leaks found (values redacted; see File/Line/RuleID above). Remove the secret;" >&2
	echo "secret-scan: if it was committed, rotate it - it stays in git history. A false positive: add its" >&2
	echo "secret-scan: Fingerprint line to .gitleaksignore. Deliberately fake test keys belong in a" >&2
	echo "secret-scan: tests/fixtures/fake-secrets/ directory (allowlisted in .gitleaks.toml)." >&2
fi
exit "$rc"
