// SPDX-License-Identifier: Apache-2.0
// Root build. Shared setup of the pure JVM modules (JUnit 5, JVM 21 bytecode, explicit API) and dependency locking.
// The Android Gradle plugin joins the classpath unless -PjvmOnly=true; its built-in Kotlin and the Kotlin plugin of
// the pure modules must be one version (libs.versions.toml).
import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import org.jetbrains.kotlin.gradle.dsl.KotlinAndroidProjectExtension
import org.jetbrains.kotlin.gradle.dsl.KotlinJvmProjectExtension

buildscript {
    // Google groups only from Google Maven, everything else only from Maven Central (regex as in settings.gradle.kts).
    repositories {
        exclusiveContent {
            forRepository { google() }
            filter {
                includeGroupByRegex(
                    """(com\.android|androidx|com\.google\.android|com\.google\.testing\.platform|""" +
                        """com\.google\.prefab)(\..+)?""",
                )
            }
        }
        mavenCentral()
    }
    // The lock file of the build classpath holds the Android plugin closure, so a -PjvmOnly=true build (which would
    // leave lock entries unresolved, a Gradle error) does not lock its classpath; verification-metadata.xml covers it.
    if (project.findProperty("jvmOnly") != "true") {
        dependencies {
            val catalog = project.extensions.getByType<VersionCatalogsExtension>().named("libs")
            classpath(catalog.findLibrary("agp").get())
        }
        configurations.classpath { resolutionStrategy.activateDependencyLocking() }
    }
}

plugins {
    base // the root `check` task that the Kotlin gates hang on
    alias(libs.plugins.kotlin.jvm) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.kotlin.compose) apply false
}

// SDK levels of every Android module (Android 17: compileSdk = targetSdk = minSdk = 37). -PtestMinSdk=36 lowers minSdk
// of the debug build type only, for emulator runs on the API 36 image: each Android module sets it in
// `beforeVariants(withBuildType("debug"))` from androidDebugMinSdk, so every other build type (release, a later
// prototype) keeps 37; the SAT tests check minSdkVersion 37 in the merged release manifest.
val testMinSdk = providers.gradleProperty("testMinSdk").orNull
require(testMinSdk == null || testMinSdk == "36") { "-PtestMinSdk accepts only 36 (the API 36 emulator image)" }
extra["androidSdk"] = 37
extra["androidDebugMinSdk"] = testMinSdk?.toInt() ?: 37
extra["androidBuildTools"] = "37.0.0"

allprojects {
    dependencyLocking {
        lockAllConfigurations()
        lockMode = LockMode.STRICT
    }
}

fun Project.useJUnit5() {
    dependencies {
        add("testImplementation", platform(libs.junit.bom))
        add("testImplementation", libs.junit.jupiter)
        add("testRuntimeOnly", libs.junit.platform.launcher)
    }
    tasks.withType<Test> { useJUnitPlatform() }
}

subprojects {
    pluginManager.withPlugin("org.jetbrains.kotlin.jvm") {
        extensions.configure<JavaPluginExtension> {
            sourceCompatibility = JavaVersion.VERSION_21
            targetCompatibility = JavaVersion.VERSION_21
        }
        extensions.configure<KotlinJvmProjectExtension> {
            explicitApi()
            compilerOptions.jvmTarget.set(JvmTarget.JVM_21)
        }
        useJUnit5()
    }
    pluginManager.withPlugin("com.android.application") { useJUnit5() }
    pluginManager.withPlugin("com.android.library") {
        useJUnit5()
        // Library modules: every public declaration states its visibility (the application module has no consumer).
        extensions.configure<KotlinAndroidProjectExtension>("kotlin") { explicitApi() }
    }
}

// Kotlin static gates (T1.107). detekt-cli and ktlint-cli run as plain JavaExec tasks on their self-contained jars, so
// each keeps its own Kotlin compiler. `check` runs both over every module's sources (ktlint also over the build
// scripts) and over the planted fixtures in config/test-fixtures/<tool>/: a fixture names the rules it must trigger
// in a `// expect: <rule>, ...` line (none: it must pass), so each rule is proven to fire and to stop at its limit.
// Rules: config/detekt.yml (on top of detekt's defaults) and .editorconfig.
val detektCli by configurations.creating {
    isCanBeConsumed = false
    isTransitive = false
}
// Type resolution (rules such as UnsafeCallOnNullableType are skipped without it) sees the analysed sources and the
// Kotlin standard library only: a `!!` on a JDK, Android or library value is not reported by detekt (`sourceRules`
// below rejects a `!!` by text, gaps in ENGINEERING.md section 2).
val detektTypes by configurations.creating {
    isCanBeConsumed = false
}
val ktlintCli by configurations.creating {
    isCanBeConsumed = false
    isTransitive = false
}
dependencies {
    addProvider("detektCli", libs.detekt.cli.map { "${it.module}:${it.version}:all" })
    detektTypes(libs.kotlin.stdlib)
    addProvider("ktlintCli", libs.ktlint.cli.map { "${it.module}:${it.version}:all" })
}

// Kotlin that tools/codegen writes, and Kotlin vendored from upstream, is not analysed. The exemption is exact: a file
// inside a listed directory whose leading comment header (first 10 lines, as tools/quality/check_size.py reads it) has
// the marker line of its kind (a pair of directories and marker: `first` and `second` below). Generated: `@generated`;
// ENGINEERING.md section 2 exempts it from the size limits too, explicit API mode still applies. Vendored (the package
// root of third_party/g2-driver): the "Copied from ..." line that tools/upstream-sync/verify-import.sh checks on every
// copied file; a file we add there is analysed. Fixtures: config/test-fixtures/source-rules/{generated,vendored}.
val generatedDirs = listOf(file("protocol/src/main/kotlin/app/hudcompanion/protocol/generated"))
val generatedMarker = Regex("""\s*//\s*@generated(\s.*)?""")
val vendoredDirs = listOf(file("third_party/g2-driver/src/main/kotlin/app/hudcompanion/g2driver"))
val vendoredMarker = Regex("""// Copied from \S+@[0-9a-f]{40}; modified by The Wayfinch Authors \(see UPSTREAM\.md\)""")
val exemptions = listOf(generatedDirs to generatedMarker, vendoredDirs to vendoredMarker)

fun Pair<List<File>, Regex>.exempts(file: File): Boolean =
    file.isFile &&
        first.any { file.startsWith(it) } &&
        file.useLines { lines ->
            val header = lines.take(10).takeWhile { it.isBlank() || it.trimStart().startsWith("//") }
            header.any { second.matches(it) }
        }

// Only the Gradle output directories are skipped: a Kotlin package may be called `build`.
val outputDirs = listOf("build/**", "*/build/**", "modules/*/build/**")
val kotlinSources =
    fileTree(projectDir) {
        include("**/src/**/*.kt")
        exclude(outputDirs)
        exclude("config/**")
        exclude { f -> exemptions.any { it.exempts(f.file) } }
    }
val buildScripts =
    fileTree(projectDir) {
        include("**/*.gradle.kts")
        exclude(outputDirs)
        exclude("config/**")
    }

// ktlint merges every .editorconfig on the path of a file (the --editorconfig option only gives defaults), so a nested
// one could switch rules off: only android/.editorconfig may exist.
fun strayEditorconfigs(root: File): Set<File> =
    fileTree(root) {
        include("**/.editorconfig")
        exclude(outputDirs)
        exclude("config/**")
    }.files - File(root, ".editorconfig")

fun requireSingleEditorconfig() {
    val stray = strayEditorconfigs(projectDir)
    check(stray.isEmpty()) { "ktlint reads nested .editorconfig files; only android/.editorconfig may exist: $stray" }
}
val detektMain = "io.gitlab.arturbosch.detekt.cli.Main"
val detektConfig = listOf("--config", "config/detekt.yml", "--build-upon-default-config")

fun detektOptions() = detektConfig + listOf("--classpath", detektTypes.asPath)
val ktlintMain = "com.pinterest.ktlint.Main"
val ktlintOptions = listOf("--editorconfig=.editorconfig", "--relative")

/** Findings per path relative to [fixtures]: group 1 of [this] is the file, group 2 the rule, per output line. */
fun Regex.findings(
    lines: List<String>,
    fixtures: File,
): Map<String, Set<String>> =
    lines
        .mapNotNull { matchEntire(it)?.destructured }
        .groupBy({ (path, _) -> file(path).relativeTo(fixtures).path }, { (_, rule) -> rule })
        .mapValues { it.value.toSet() }

// Text rules for what detekt cannot see (see detektTypes). lex() blanks comments and the text of string and char
// literals and backtick names; the expressions of `${...}` templates are code and stay. It returns the code and, apart,
// the line comments (the reason of a suppression). Kotlin nests block comments; the lexer does not (gap S1, T1.282).
val codeToken = Regex("""//[^\n]*|/\*[\s\S]*?\*/|\x22{3}|\x22|'(?:\\u[0-9a-fA-F]{4}|\\.|[^'\\\n])'|`[^`\n]*`|[{}]""")
val stringToken = Regex("""\\[\s\S]|\x22|\$\{|\n""")
val rawToken = Regex("""\x22+|\$\{""")
val tokens = listOf(codeToken, stringToken, rawToken) // by mode: code, inString, inRaw
val inString = -1
val inRaw = -2

fun lex(text: String): Pair<String, String> {
    val code = CharArray(text.length) { if (text[it] == '\n') '\n' else ' ' }
    val comments = code.copyOf()

    // Per open construct: the unclosed braces of a code frame (a template counts its own `{`; the top level never
    // closes), or inString / inRaw.
    val frames = arrayListOf(1 shl 30)

    fun pop() = frames.removeAt(frames.lastIndex)

    fun close() {
        frames[frames.lastIndex]--
        if (frames.last() == 0) pop()
    }

    var i = 0
    while (i < text.length) {
        val mode = frames.last()
        val token = tokens[maxOf(0, -mode)].find(text, i)
        val start = token?.range?.first ?: text.length
        if (mode > 0) text.toCharArray(code, i, i, start)
        val t = token?.value ?: break
        i = start + t.length
        if (t == "{" || t == "}") code[start] = t[0]
        // in a raw string a run of quotes closes it with its last three
        when {
            t == "\${" -> frames.add(1)
            mode == inString -> if (t == "\"" || t == "\n") pop()
            mode == inRaw -> if (t.length >= 3 && t[0] == '"') pop()
            t == "{" -> frames[frames.lastIndex]++
            t == "}" -> close()
            t.startsWith("//") -> text.toCharArray(comments, start, start, i)
            t == "\"\"\"" -> frames.add(inRaw)
            t == "\"" -> frames.add(inString)
        }
    }
    return String(code) to String(comments)
}

// A suppression of detekt or ktlint by annotation: plain, qualified, with a use-site target, in brackets, or
// SuppressWarnings (which both tools honour).
val suppression = Regex("""@(?:\w+:)?(?:\w+\.)*Suppress(?:Warnings)?\b|@\[[^\]]*\bSuppress""")
val suppressionReason = Regex("""//\s*\S""")

/** Rule ids with their 1-based lines: `NotNullAssertion` (`!!` on any type) and `SuppressWithoutReason`. */
fun textFindings(file: File): List<Pair<Int, String>> {
    val (code, comments) = lex(file.readText())
    val reasons = comments.lines()
    return code.lines().flatMapIndexed { index, line ->
        val unexplained = suppression.containsMatchIn(line) && !suppressionReason.containsMatchIn(reasons[index])
        listOfNotNull(
            "NotNullAssertion".takeIf { "!!" in line },
            "SuppressWithoutReason".takeIf { unexplained },
        ).map { index + 1 to it }
    }
}

/** Fails unless every fixture has exactly the findings of its `// expect:` line (none without one). */
fun checkFixtures(
    tool: String,
    fixtures: File,
    found: Map<String, Set<String>>,
) {
    val files = fixtures.walkTopDown().filter { it.extension == "kt" }.toList()
    val expect = Regex("""^// expect: (.+)$""", RegexOption.MULTILINE)
    val wrong =
        files.mapNotNull { file ->
            val line = expect.find(file.readText())?.groupValues?.get(1)
            val want =
                line
                    ?.split(",")
                    .orEmpty()
                    .map { it.trim() }
                    .toSet()
            val key = file.relativeTo(fixtures).path
            val got = found[key].orEmpty()
            if (want == got) null else "$key: expected $want, $tool reported $got"
        }
    check(files.any { expect.containsMatchIn(it.readText()) }) { "$tool: no planted violation in $fixtures" }
    check(wrong.isEmpty()) { "$tool fixtures differ from their expectations:\n" + wrong.joinToString("\n") }
}

fun JavaExec.gate(
    text: String,
    cli: Configuration,
    main: String,
) {
    group = "verification"
    description = text
    classpath = cli
    mainClass = main
}

val detekt by tasks.registering(JavaExec::class) {
    gate("detekt over the Kotlin sources of every module", detektCli, detektMain)
    inputs.files(kotlinSources).withPropertyName("sources")
    inputs.file("config/detekt.yml").withPropertyName("config")
    doFirst { args(detektOptions() + listOf("--input", kotlinSources.joinToString(","))) }
}

val ktlintCheck by tasks.registering(JavaExec::class) {
    gate("ktlint over the Kotlin sources and build scripts of every module", ktlintCli, ktlintMain)
    inputs.files(kotlinSources, buildScripts).withPropertyName("sources")
    inputs.file(".editorconfig").withPropertyName("editorconfig")
    doFirst {
        requireSingleEditorconfig()
        args(ktlintOptions + (kotlinSources + buildScripts).files.map { it.absolutePath })
    }
}

tasks.register<JavaExec>("ktlintFormat") {
    gate("ktlint --format over the files of ktlintCheck (rewrites them)", ktlintCli, ktlintMain)
    group = "formatting"
    doFirst {
        requireSingleEditorconfig()
        args(ktlintOptions + "--format" + (kotlinSources + buildScripts).files.map { it.absolutePath })
    }
}

// Rules written as text because detekt cannot apply them (a `!!` on a JDK, Android or library value; a @Suppress
// without a reason). Fixtures: config/test-fixtures/source-rules/text, run by sourceRuleFixtures.
val sourceRules by tasks.registering {
    group = "verification"
    description = "No !! on any type and no @Suppress without a reason, by text over every module's sources"
    inputs.files(kotlinSources).withPropertyName("sources")
    doLast {
        val found =
            kotlinSources.files.sorted().flatMap { file ->
                textFindings(file).map { (line, rule) -> "${file.relativeTo(projectDir)}:$line: $rule" }
            }
        check(found.isEmpty()) { "source rules failed:\n" + found.joinToString("\n") }
    }
}

val detektFixtures by tasks.registering(JavaExec::class) {
    gate("detekt over the planted fixtures (exit 2 = findings)", detektCli, detektMain)
    val fixtures = file("config/test-fixtures/detekt")
    val console = java.io.ByteArrayOutputStream()
    isIgnoreExitValue = true
    standardOutput = console
    inputs.dir(fixtures).withPropertyName("fixtures")
    inputs.file("config/detekt.yml").withPropertyName("config")
    outputs.upToDateWhen { false }
    doFirst {
        console.reset()
        args(detektOptions() + listOf("--input", "$fixtures"))
    }
    doLast {
        // The bad fixtures guarantee findings: exit 0 means the gate was switched off (for example maxIssues).
        val exit = executionResult.get().exitValue
        check(exit == 2) { "detekt must report the planted violations and exit 2 (exit $exit)" }
        val lines = console.toString().lines()
        logger.lifecycle(lines.joinToString("\n"))
        val line = Regex("""(\S+\.kt):\d+:\d+: .*\[(\w+)]""")
        checkFixtures("detekt", fixtures, line.findings(lines, fixtures))
    }
}

val ktlintFixtures by tasks.registering(JavaExec::class) {
    gate("ktlint over the planted fixtures (exit 1 = findings)", ktlintCli, ktlintMain)
    val fixtures = file("config/test-fixtures/ktlint")
    val report = layout.buildDirectory.file("gates/ktlint-fixtures.txt")
    isIgnoreExitValue = true
    inputs.dir(fixtures).withPropertyName("fixtures")
    inputs.file(".editorconfig").withPropertyName("editorconfig")
    outputs.file(report)
    doFirst {
        report.get().asFile.delete()
        val files = fixtures.walkTopDown().filter { it.extension == "kt" }.map { it.absolutePath }
        args(ktlintOptions + "--reporter=plain,output=${report.get()}" + files)
    }
    doLast {
        val exit = executionResult.get().exitValue
        check(exit == 1) { "ktlint must report the planted violations and exit 1 (exit $exit)" }
        val line = Regex("""(\S+\.kt):\d+:\d+: .*\((\S+:\S+)\).*""")
        val lines =
            report
                .get()
                .asFile
                .takeIf { it.isFile }
                ?.readLines()
                .orEmpty()
        checkFixtures("ktlint", fixtures, line.findings(lines, fixtures))
    }
}

// Fixtures of the rules that live in this script: the text rules, the generated and vendored exemptions (a marked file
// in the listed directory is exempt; unmarked, late-marked, marked-elsewhere files are not), the stray .editorconfig.
val sourceRuleFixtures by tasks.registering {
    group = "verification"
    description = "Planted fixtures of the build-script rules: text rules, generated exemption, stray .editorconfig"
    val root = file("config/test-fixtures/source-rules")
    inputs.dir(root).withPropertyName("fixtures")
    doLast {
        val text = File(root, "text")
        val rules = text.walkTopDown().filter { it.extension == "kt" }
        val relative = { f: File, base: File -> f.relativeTo(base).path }
        val found = rules.associate { relative(it, text) to textFindings(it).map { (_, rule) -> rule }.toSet() }
        checkFixtures("source rules", text, found)
        listOf("generated" to generatedMarker, "vendored" to vendoredMarker).forEach { (name, marker) ->
            val dir = File(root, name)
            val rule = listOf(File(dir, "listed")) to marker
            val exempt = dir.walkTopDown().filter { it.extension == "kt" && rule.exempts(it) }
            checkFixtures("$name exemption", dir, exempt.associate { relative(it, dir) to setOf("exempt") })
        }
        val stray = strayEditorconfigs(File(root, "editorconfig"))
        check(stray == setOf(File(root, "editorconfig/bad/.editorconfig"))) { "stray .editorconfig finder: $stray" }
    }
}

tasks.check { dependsOn(detekt, ktlintCheck, detektFixtures, ktlintFixtures, sourceRules, sourceRuleFixtures) }
