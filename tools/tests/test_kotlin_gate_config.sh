#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
#
# Static tests of the Kotlin gates (T1.107) that need no JDK: the detekt thresholds, the planted fixtures and the
# wiring in android/build.gradle.kts. The full Gradle run (`make test-android`) proves that detekt and ktlint react to the
# fixtures; this proves the fixtures sit exactly on the ENGINEERING.md section 2 limits, that the thresholds are
# the limit plus one, that every active rule has a fixture, and that `check` still depends on all six gate tasks.
# Usage: sh tools/tests/test_kotlin_gate_config.sh [ROOT]   (ROOT defaults to the repository top)
set -u

ROOT=${1:-$(cd "$(dirname "$0")/../.." && pwd)}
CFG=$ROOT/android/config/detekt.yml
BUILD=$ROOT/android/build.gradle.kts
FIX=$ROOT/android/config/test-fixtures
fail=0

ok() { printf 'ok   %s\n' "$1"; }
bad() {
	printf 'FAIL %s\n' "$1"
	fail=1
}
check() { # check NAME COMMAND...: ok when the command succeeds
	name=$1
	shift
	if "$@" >/dev/null 2>&1; then ok "$name"; else bad "$name"; fi
}

for f in "$CFG" "$BUILD" "$ROOT/mk/android.mk"; do
	[ -f "$f" ] || {
		bad "missing $f"
		exit 1
	}
done
[ -d "$FIX/detekt/bad" ] && [ -d "$FIX/detekt/ok" ] && [ -d "$FIX/ktlint/bad" ] && [ -d "$FIX/ktlint/ok" ] || {
	bad "fixture directories missing"
	exit 1
}

# value of KEY under the rule RULE of the detekt config (first match after the rule line, within 4 lines)
cfg_value() { # cfg_value RULE KEY
	awk -v rule="$1" -v key="$2" '
		$0 ~ "^  " rule ":" { seen = NR; next }
		seen && NR <= seen + 5 && $1 == key ":" { print $2; exit }' "$CFG"
}

# --- thresholds are the ENGINEERING.md section 2 hard limit plus one (detekt reports at "value >= threshold")
want_threshold() { # want_threshold RULE KEY VALUE
	got=$(cfg_value "$1" "$2")
	if [ "$got" = "$3" ]; then ok "$1 $2 is $3"; else bad "$1 $2 is '$got', want $3 (ENGINEERING.md section 2 limit + 1)"; fi
}
want_threshold CyclomaticComplexMethod threshold 11
want_threshold LongMethod threshold 61
want_threshold LongParameterList functionThreshold 5
want_threshold LongParameterList constructorThreshold 5
want_threshold LongParameterList ignoreDataClasses true
want_threshold NestedBlockDepth threshold 4
want_threshold MaxLineLength maxLineLength 120

# --- the rules of the task stay active
for rule in CyclomaticComplexMethod LongMethod LongParameterList NestedBlockDepth UnsafeCallOnNullableType \
	TooGenericExceptionCaught GlobalCoroutineUsage UnusedPrivateMember; do
	if [ "$(cfg_value "$rule" active)" = "true" ]; then ok "$rule is active"; else bad "$rule is not active"; fi
done

# --- the config cannot weaken the gate from inside: no build section (maxIssues), no baseline, no global excludes
if grep -Eq '^(build|processors|baseline)\b|maxIssues|baseline' "$CFG"; then
	bad "detekt.yml carries build/maxIssues/baseline settings (a loosened maxIssues keeps detekt exit 0 on findings)"
else ok "detekt.yml has no build/maxIssues/baseline settings"; fi

# --- the forbidden import is the one class, not its package (android.util.Size is allowed by SAT-PL-10)
if grep -Eq "^    imports: \['android\.util\.Log', 'android\.util\.Log\.\*'\]" "$CFG"; then ok "ForbiddenImport bans android.util.Log and its members only"
else bad "ForbiddenImport imports differ from ['android.util.Log', 'android.util.Log.*'] (a package pattern bans android.util.Size too; the member pattern catches import android.util.Log.d)"; fi
if grep -q '^import android.util.Size' "$FIX/detekt/ok/AndroidUtilSize.kt"; then ok "ok fixture imports android.util.Size"
else bad "no ok fixture pins android.util.Size"; fi

# --- every rule the config switches on has a bad fixture that names it, and a boundary rule also an ok fixture
rules=$(awk '/^  [A-Za-z]+:/ { r = $1; sub(":", "", r) } /^    active: true/ { print r }' "$CFG")
[ -n "$rules" ] || bad "no active rule found in detekt.yml (parser returned nothing)"
for rule in $rules; do
	if grep -lq "^// expect:.*\b$rule\b" "$FIX"/detekt/bad/*.kt 2>/dev/null; then
		ok "bad fixture for $rule"
	else bad "no bad fixture expects $rule"; fi
done
for rule in CyclomaticComplexMethod LongMethod LongParameterList NestedBlockDepth MaxLineLength; do
	set -- "$FIX"/detekt/ok/"$rule"*.kt
	if [ -f "$1" ]; then ok "ok fixture for $rule"; else bad "no ok fixture for $rule"; fi
done

# --- fixture hygiene: SPDX first line; a bad fixture has one expect line, an ok fixture none
for f in "$FIX"/detekt/*/*.kt "$FIX"/ktlint/*/*.kt "$FIX"/source-rules/text/*/*.kt; do
	n=$(basename "$f")
	case $(sed -n 1p "$f") in "// SPDX-License-Identifier: Apache-2.0") ;; *) bad "$n has no SPDX first line" ;; esac
	count=$(grep -c '^// expect:' "$f")
	case $f in
	*/bad/*) [ "$count" -eq 1 ] || bad "$n: $count expect lines, want 1" ;;
	*/ok/*) [ "$count" -eq 0 ] || bad "$n: ok fixture carries an expect line" ;;
	esac
done
ok "fixture headers and expect lines scanned"

# --- the fixtures sit exactly on the limits, measured here without detekt
fun_lines() { awk '/^fun /{s=NR} s && /^}/{print NR-s+1; exit}' "$1"; }
max_depth() { awk '/^fun /{d=-1} { n=gsub(/\{/,"{"); m=gsub(/\}/,"}"); d+=n; if (d>mx) mx=d; d-=m } END{print mx}' "$1"; }
ifs() { grep -c '^ *if (' "$1"; }
params() { awk -v pat="${2:-^(data )?(fun|class) }" '$0 ~ pat { s=$0; sub(/^[^(]*\(/,"",s); sub(/\).*$/,"",s); print gsub(/,/,",",s)+1; exit }' "$1"; }
longest() { awk '{ if (length($0) > mx) mx = length($0) } END { print mx }' "$1"; }

want() { # want NAME GOT WANT
	if [ "$2" = "$3" ]; then ok "$1 = $3"; else bad "$1 is '$2', want $3"; fi
}
D=$FIX/detekt
want "LongMethod60 function lines" "$(fun_lines "$D/ok/LongMethod60.kt")" 60
want "LongMethod61 function lines" "$(fun_lines "$D/bad/LongMethod61.kt")" 61
want "CyclomaticComplexMethod10 ifs" "$(ifs "$D/ok/CyclomaticComplexMethod10.kt")" 9
want "CyclomaticComplexMethod11 ifs" "$(ifs "$D/bad/CyclomaticComplexMethod11.kt")" 10
want "NestedBlockDepth3 depth" "$(max_depth "$D/ok/NestedBlockDepth3.kt")" 3
want "NestedBlockDepth4 depth" "$(max_depth "$D/bad/NestedBlockDepth4.kt")" 4
want "LongParameterList4 params" "$(params "$D/ok/LongParameterList4.kt" '^fun ')" 4
want "LongParameterList4 constructor params" "$(params "$D/ok/LongParameterList4.kt" '^class ')" 4
want "LongParameterListFunction5 params" "$(params "$D/bad/LongParameterListFunction5.kt")" 5
want "LongParameterListConstructor5 params" "$(params "$D/bad/LongParameterListConstructor5.kt")" 5
want "detekt MaxLineLength120 longest line" "$(longest "$D/ok/MaxLineLength120.kt")" 120
want "detekt MaxLineLength121 longest line" "$(longest "$D/bad/MaxLineLength121.kt")" 121
want "ktlint MaxLineLength120 longest line" "$(longest "$FIX/ktlint/ok/MaxLineLength120.kt")" 120
want "ktlint MaxLineLength121 longest line" "$(longest "$FIX/ktlint/bad/MaxLineLength121.kt")" 121
data=$(grep -c '^data class' "$D/ok/LongParameterListDataClass6.kt")
want "data-class fixture is a data class" "$data" 1
want "data-class fixture parameters" "$(params "$D/ok/LongParameterListDataClass6.kt")" 6
if grep -q '!!' "$D/bad/UnsafeCallOnNullableType.kt"; then ok "the not-null assertion fixture contains !!"; else bad "the !! fixture has no !!"; fi
# detekt's LongMethod counts lines of code: 60 code lines pass however many blank and comment lines surround them
B=$D/ok/LongMethodBlankAndCommentLines.kt
code=$(awk '/^fun /{s=1} s && !/^[ \t]*(\/\/.*)?$/{n++} s && /^}/{print n; exit}' "$B")
want "LongMethodBlankAndCommentLines lines of code" "$code" 60
span=$(fun_lines "$B")
if [ "$span" -gt 60 ]; then ok "LongMethodBlankAndCommentLines spans $span physical lines (> 60)"; else bad "LongMethodBlankAndCommentLines spans only $span physical lines"; fi

# --- wiring: `check` hangs on all gate tasks, the tasks exist, and test-android runs `check`
for task in detekt ktlintCheck detektFixtures ktlintFixtures sourceRules sourceRuleFixtures; do
	if grep -Eq "^val $task by tasks.registering" "$BUILD"; then ok "$task is registered"; else bad "$task is not registered"; fi
	deps=$(grep -E '^tasks\.check *\{' "$BUILD")
	case $deps in *"$task"*) ok "check depends on $task" ;; *) bad "check does not depend on $task" ;; esac
done
# the fixture tasks require the exit code the planted violations give: a tolerated exit 0 would let a loosened
# maxIssues (or any other way of keeping findings from failing the build) pass every gate
if grep -Fq 'check(exit == 2)' "$BUILD" && grep -Fq 'check(exit == 1)' "$BUILD" && ! grep -Eq 'exit == 0 *\|\|' "$BUILD"; then
	ok "detekt fixtures require exit 2, ktlint fixtures exit 1"
else bad "the fixture tasks accept another exit code than 2 (detekt) / 1 (ktlint)"; fi
# only Gradle output directories are skipped: a Kotlin package may be called build
if grep -Fq '**/build/**' "$BUILD" "$ROOT/android/app/build.gradle.kts"; then bad "a glob skips every directory named build"
else ok "no **/build/** glob in the gate sources"; fi
if grep -Fq 'val outputDirs = listOf("build/**", "*/build/**", "modules/*/build/**")' "$BUILD"; then ok "outputDirs lists the Gradle output dirs"
else bad "outputDirs changed"; fi
# the generated-code exemption is the listed directory plus the marker, never a name pattern
if grep -Fq '"/generated/" in' "$BUILD"; then bad "isGenerated exempts any directory named generated"; else ok "isGenerated has no name pattern"; fi
if grep -Fq 'val generatedDirs = listOf(file("protocol/src/main/kotlin/app/hudcompanion/protocol/generated"))' "$BUILD"; then
	ok "generatedDirs lists the one emitter output directory"
else bad "generatedDirs changed (the exemption must stay the exact emitter output directory)"; fi
# ktlint must refuse a nested .editorconfig before it runs
n=$(grep -c 'requireSingleEditorconfig()' "$BUILD")
if [ "$n" -ge 3 ]; then ok "ktlintCheck and ktlintFormat call requireSingleEditorconfig"; else bad "requireSingleEditorconfig is not called by both ktlint tasks ($n uses)"; fi
if grep -Eq 'gradlew .*[[:space:]]check([[:space:]]|$)' "$ROOT/mk/android.mk"; then ok "test-android runs check"; else bad "test-android does not run check"; fi
for opt in '"--build-upon-default-config"' '"--classpath"'; do
	if grep -q -- "$opt" "$BUILD"; then ok "detekt is called with $opt"; else bad "detekt lost $opt (type resolution or default config)"; fi
done

# --- the source sets the gates read: every module's src tree and every build script, none of the fixtures
for glob in 'include("**/src/**/*.kt")' 'include("**/*.gradle.kts")'; do
	if grep -qF -- "$glob" "$BUILD"; then ok "build.gradle.kts selects $glob"; else bad "build.gradle.kts lost $glob (the gates would read no sources)"; fi
done

# --- ktlint settings the ktlint fixtures are written for
for line in 'max_line_length = 120' 'indent_size = 4' 'ktlint_code_style = ktlint_official'; do
	if grep -qx "$line" "$ROOT/android/.editorconfig"; then ok ".editorconfig has '$line'"; else bad ".editorconfig lost '$line'"; fi
done

# --- a rule exclude hides real findings while every fixture (under config/) stays green: only the placeholder test may be
# excluded, and ForbiddenImport bans android.util.Log alone (android.util.Size stays allowed)
n=$(grep -cE '^[[:space:]]*(excludes|includes):' "$CFG")
if [ "$n" -eq 1 ] && grep -Fq "excludes: ['**/ModuleTest.kt']" "$CFG"; then ok "detekt.yml has no exclude but the placeholder test's"
else bad "detekt.yml has $n excludes/includes entries; only EmptyFunctionBlock's '**/ModuleTest.kt' is allowed"; fi
if grep -Fq "imports: ['android.util.Log', 'android.util.Log.*']" "$CFG"; then ok "ForbiddenImport bans android.util.Log and its members alone"
else bad "ForbiddenImport imports changed (android.util.Size must stay allowed, android.util.Log.d stay banned)"; fi
if grep -Fxq 'import android.util.Size' "$D/ok/AndroidUtilSize.kt"; then ok "an ok fixture imports android.util.Size"
else bad "no ok fixture imports android.util.Size"; fi

# --- the generated-code exemption has its four negatives and its positive, and the editorconfig finder its stray file
G=$FIX/source-rules/generated
for f in listed/ListedMarked listed/ListedNoMarker listed/ListedMention listed/ListedLateMarker elsewhere/generated/OtherDirMarked; do
	check "generated fixture $f exists" test -f "$G/$f.kt"
done
check "stray .editorconfig fixture exists" test -f "$FIX/source-rules/editorconfig/bad/.editorconfig"
exempt=$(grep -rl '^// expect: exempt$' "$G" | sed 's|.*/||')
want "only the marked file of the listed dir is expected exempt" "$exempt" "ListedMarked.kt"

# --- the vendored-code exemption (T1.84): two exact package directories plus the provenance line the import puts on
# every copied file, wired next to the generated exemption, with its negatives, and it covers exactly the files
# UPSTREAM.md lists as `source` (the copied Kotlin files), never a file we add
VD=$FIX/source-rules/vendored
for f in listed/VendoredMarked listed/VendoredNoMarker listed/VendoredMention listed/VendoredLateMarker \
	listed/VendoredShortCommit listed/VendoredOtherAuthor elsewhere/OtherDirMarked; do
	check "vendored fixture $f exists" test -f "$VD/$f.kt"
done
exempt=$(grep -rl '^// expect: exempt$' "$VD" | sed 's|.*/||')
want "only the marked file of the listed dir is expected vendored-exempt" "$exempt" "VendoredMarked.kt"
n=$(grep -c 'file("third_party/' "$BUILD")
want "vendoredDirs lists exactly one directory" "$n" 1
if grep -Fxq 'val vendoredDirs = listOf(file("third_party/g2-driver/src/main/kotlin/app/hudcompanion/g2driver"))' "$BUILD"; then
	ok "vendoredDirs lists the g2driver package root"
else bad "vendoredDirs changed (the exemption must stay the exact directory of the copied files)"; fi
if grep -Fxq 'val vendoredMarker = Regex("""// Copied from \S+@[0-9a-f]{40}; modified by The Wayfinch Authors \(see UPSTREAM\.md\)""")' "$BUILD"; then
	ok "vendoredMarker is the provenance line with a 40-hex commit and the author line"
else bad "vendoredMarker changed (it must match the line verify-import.sh checks on every copied file)"; fi
if grep -Fxq 'val exemptions = listOf(generatedDirs to generatedMarker, vendoredDirs to vendoredMarker)' "$BUILD" &&
	grep -Fq 'exclude { f -> exemptions.any { it.exempts(f.file) } }' "$BUILD"; then ok "the gate sources skip generated and vendored files, each with its own marker"
else bad "kotlinSources no longer skips both kinds by directory plus marker (or pairs a directory with the other marker)"; fi
n=$(grep -c 'exempts(' "$BUILD")
want "exempts: definition, gate sources, fixtures" "$n" 3
V=$ROOT/android/third_party/g2-driver
PROV='^// Copied from [^[:space:]]+@[0-9a-f]{40}; modified by The Wayfinch Authors \(see UPSTREAM\.md\)$'
sources=$(awk -F'|' 'NF == 6 && $4 ~ /source/ { gsub(/[ `]/, "", $2); print $2 }' "$V/UPSTREAM.md" 2>/dev/null)
if [ -n "$sources" ]; then ok "UPSTREAM.md lists source files"; else bad "no source row found in the vendored module's UPSTREAM.md"; fi
for f in $sources; do
	case "$f" in
	src/main/kotlin/app/hudcompanion/g2driver/*) ok "$f sits under the vendoredDirs directory" ;;
	*) bad "$f is a source row but outside vendoredDirs (the Kotlin gates would analyse it)" ;;
	esac
	if sed -n 2p "$V/$f" | grep -Eq "$PROV"; then ok "$f carries the provenance line (the Kotlin gates skip it)"
	else bad "$f lacks the provenance line on line 2 (the Kotlin gates would analyse it)"; fi
done
for f in $(cd "$V" && find src -name '*.kt'); do
	if head -n 10 "$V/$f" | grep -Eq "$PROV" && ! printf '%s\n' "$sources" | grep -Fxq "$f"; then
		bad "$f carries the provenance line but is not a source row of UPSTREAM.md (it would escape the Kotlin gates)"
	fi
done
# the stray .editorconfig fixture must run the finder the gate runs (one include, one function, three uses), not a copy
n=$(grep -c 'include("\*\*/.editorconfig")' "$BUILD")
want "the .editorconfig finder is written once" "$n" 1
n=$(grep -c 'strayEditorconfigs(' "$BUILD")
want "strayEditorconfigs: definition, requireSingleEditorconfig, fixture" "$n" 3
# fixtures are keyed by their path under the fixture root, not by bare file name
if grep -Fq 'File(path).name' "$BUILD" || grep -Fq 'file.name' "$BUILD"; then bad "fixture findings are keyed by bare file name"
else ok "fixture findings are keyed by relative path"; fi

# --- every bypass of the text rules that was found has its planted negative (each must fail sourceRuleFixtures)
T=$FIX/source-rules/text/bad
for f in NotNullAssertionInTemplate NotNullAssertionInRawTemplate NotNullAssertionAfterRawStringEndingInQuote \
	NotNullAssertionAfterBacktickSlashes NotNullAssertionAfterBacktickQuote SuppressReasonInString SuppressReasonAsArgument \
	SuppressQualified SuppressWarningsWithoutReason SuppressBracketWithoutReason SuppressGetTargetWithoutReason \
	SuppressFieldTargetWithoutReason SuppressParamTargetWithoutReason SuppressPropertyTargetWithoutReason; do
	check "text rule negative $f" test -f "$T/$f.kt"
done
check "detekt negative for a static member import of android.util.Log" test -f "$D/bad/ForbiddenImportStaticMember.kt"
check "no known-gaps fixtures left (every gap is closed)" test ! -e "$FIX/source-rules/known-gaps"

[ "$fail" -eq 0 ] && printf '%s\n' "all kotlin gate config tests passed"
exit "$fail"
