# SPDX-License-Identifier: Apache-2.0
# Android build targets (T1.17a). Gradle runs only where a JDK (and for test-android the Android SDK) exists, so
# test-android-jvm and test-android are NOT in TEST_TARGETS (they need a JDK, and test-android the Android SDK, which `make test` does not require); both stop
# with exit 2 and one line instead of a Gradle stack trace. check-android-wrapper needs no JDK and IS in TEST_TARGETS.
# Standalone: make -f mk/android.mk check-android-wrapper
.PHONY: test-android-jvm test-android check-android-wrapper check-kotlin-gate-config
TEST_TARGETS += check-android-wrapper check-kotlin-gate-config

#: test-android-jvm — Gradle tests of the pure Kotlin modules, no Android SDK (needs a JDK; not in `make test`)
test-android-jvm:
	@command -v java >/dev/null 2>&1 || [ -x "$$JAVA_HOME/bin/java" ] || { echo "test-android-jvm: needs JAVA_HOME or java on PATH" >&2; exit 2; }
	cd android && ./gradlew -PjvmOnly=true --no-daemon test

#: test-android — full Android build, tests, lint and `check` with detekt, ktlint and their planted fixtures (needs a JDK and ANDROID_HOME; not in `make test`)
test-android:
	@command -v java >/dev/null 2>&1 || [ -x "$$JAVA_HOME/bin/java" ] || { echo "test-android: needs JAVA_HOME or java on PATH" >&2; exit 2; }
	@[ -n "$$ANDROID_HOME" ] || { echo "test-android: needs ANDROID_HOME" >&2; exit 2; }
	cd android && ./gradlew --no-daemon assembleDebug test lint check

#: check-android-wrapper — the Gradle wrapper keeps its pinned URL and distribution SHA-256 (each key once) and the SHA-256 of its four files
check-android-wrapper:
	sh tools/tests/test_check_android_wrapper.sh
	sh tools/check-android-wrapper.sh

#: check-kotlin-gate-config — the Kotlin gates keep the ENGINEERING.md limits (thresholds, planted fixtures on the boundaries, `check` wired to all six gate tasks); static, no JDK
check-kotlin-gate-config:
	sh tools/tests/test_kotlin_gate_config.sh
