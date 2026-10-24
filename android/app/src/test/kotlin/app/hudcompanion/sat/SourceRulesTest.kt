// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.sat

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.io.File

/** SAT-PL-10 (banned APIs in shipped sources), the placeholder activities and SAT-PL-11 (lock files). */
class SourceRulesTest {
    private val trustType = """(javax\.net\.ssl\.)?(X509(Extended)?TrustManager|HostnameVerifier)"""
    private val banned =
        listOf(
            Regex("""\b(Base|Path|InMemory|DelegateLast)?DexClassLoader\b"""),
            Regex("""\bPathClassLoader\b"""),
            Regex("""\baddJavascriptInterface\b"""),
            Regex("""\bsetWebContentsDebuggingEnabled\b"""),
            // Own trust managers and hostname verifiers (T1.24a pins in OkHttp, it never needs either): Kotlin class
            // or object, Java implements or anonymous class, and any verifier handed over (block, lambda,
            // reference, setter).
            Regex("""(\b(class|object)\b[^{\n]*[:,]|\bimplements\b[^{\n]*)\s*$trustType\b"""),
            Regex("""\bnew\s+$trustType\s*\("""),
            Regex("""\b[Hh]ostnameVerifier\s*[{(]"""),
            Regex("""\bset(Default)?HostnameVerifier\b"""),
            Regex("""\bHttpLoggingInterceptor\b"""),
            // Native code: no vendored driver exists yet; its LC3 path gets the one exception when it lands
            // (SAT-PL-10).
            Regex("""\bSystem\.load(Library)?\b"""),
            Regex("""\bRuntime\b[^\n]*\.load(Library)?\b"""),
            // SAT-PL-09: one app logger; until it exists android.util.Log is banned everywhere, a wildcard import of
            // android.util included (detekt rule: T1.107).
            Regex("""\bandroid\.util\.(Log\b|\*)"""),
        )

    private fun isShipped(file: File): Boolean {
        val sourceSet =
            file
                .relativeTo(androidRoot)
                .invariantSeparatorsPath
                .split("/src/")
                .getOrNull(1) ?: return false
        return !sourceSet.startsWith("test") && !sourceSet.startsWith("androidTest")
    }

    /** Shipped source sets: everything under <module>/src except the test source sets. */
    private fun shippedSources(): List<File> =
        sourceFiles().filter { it.extension == "kt" || it.extension == "java" }.filter { isShipped(it) }.toList()

    private fun bannedIn(text: String): List<String> = banned.filter { it.containsMatchIn(text) }.map { it.pattern }

    private val plants =
        listOf(
            "val l = dalvik.system.DexClassLoader(p, o, null, c)",
            "val c = InMemoryDexClassLoader(b, null)",
            "class T : X509TrustManager {",
            "public final class T implements Runnable, X509TrustManager {",
            "TrustManager[] all = { new X509TrustManager() {",
            "return new javax.net.ssl.X509ExtendedTrustManager() {",
            "object : javax.net.ssl.HostnameVerifier {",
            "builder.hostnameVerifier { _, _ -> true }",
            "builder.hostnameVerifier((h, s) -> true);",
            "builder.hostnameVerifier(::allowAll)",
            "builder.hostnameVerifier(new HostnameVerifier() {",
            "val v = HostnameVerifier(::allowAll)",
            "HttpsURLConnection.setDefaultHostnameVerifier(v)",
            "connection.setHostnameVerifier(v)",
            "client.addInterceptor(HttpLoggingInterceptor())",
            "web.addJavascriptInterface(o, \"x\")",
            "WebView.setWebContentsDebuggingEnabled(true)",
            "System.loadLibrary(\"x\")",
            "System.load(\"/data/local/tmp/x.so\")",
            "Runtime.getRuntime().load(\"/data/local/tmp/x.so\")",
            "Runtime.getRuntime().loadLibrary(\"x\")",
            "android.util.Log.d(\"t\", token)",
            "import android.util.*",
        )

    private val allowed =
        listOf(
            "val tm: X509TrustManager = factory.trustManagers.single() as X509TrustManager",
            "val cores = Runtime.getRuntime().availableProcessors()",
            "import android.util.Size",
            "val pinner = CertificatePinner.Builder().add(host, pin).build()",
        )

    @Test
    fun `SAT-PL-10 shipped sources use no class loaders, trust managers, webview bridges or raw logging`() {
        val sources = shippedSources()
        assertTrue(sources.any { it.name == "EnrollActivity.kt" }, "scan found no sources: ${sources.size}")
        assertEquals(emptyList<String>(), sources.flatMap { f -> bannedIn(f.readText()).map { "${f.name}: $it" } })
        plants.forEach { assertTrue(bannedIn(it).isNotEmpty(), "planted negative passed: $it") }
        allowed.forEach { assertEquals(emptyList<String>(), bannedIn(it), "allowed line flagged: $it") }
    }

    private val readsIntent = Regex("""(?i)\b(intent|getIntent|data|extras|getData|getExtras|dataString|clipData)\b""")

    private fun placeholder(path: String) = File(androidRoot, path).readText()

    @Test
    fun `placeholder activities read nothing from the intent and EnrollActivity is FLAG_SECURE`() {
        val main = placeholder("shell/src/main/kotlin/app/hudcompanion/shell/MainActivity.kt")
        val enroll = placeholder("app/src/main/kotlin/app/hudcompanion/enroll/EnrollActivity.kt")
        for (text in listOf(main, enroll)) {
            assertEquals(null, readsIntent.find(text)?.value)
            assertTrue("finish()" in text)
            assertTrue(readsIntent.containsMatchIn(text.plant("finish()", "val u = intent.data\n        finish()")))
        }
        val secure = "window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)"
        assertTrue(secure in enroll)
    }

    private val testOnly =
        setOf(
            "com.squareup.okhttp3:mockwebserver",
            "com.squareup.okhttp3:mockwebserver3",
            "com.squareup.okhttp3:okhttp-tls",
        )

    /** Lock file lines `group:name:version=conf,conf` of a test-only library in a non-test configuration. */
    private fun shippedTestLibraries(lockfile: String): Pair<Int, List<String>> {
        val entries =
            lockfile
                .lines()
                .filter { '=' in it && !it.startsWith("#") }
                .map { it.substringBefore('=') to it.substringAfter('=').split(',') }
                .filter { (coord, _) -> coord.substringBeforeLast(':') in testOnly }
        val shipped =
            entries.flatMap { (coord, confs) ->
                confs
                    .filterNot {
                        it.startsWith(
                            "test",
                        ) || "UnitTest" in it || "AndroidTest" in it
                    }.map { "$coord in $it" }
            }
        return entries.size to shipped
    }

    /**
     * SAT-PL-11. The first net is Gradle's lock state: moving MockWebServer to `implementation` already fails the build
     * at dependency resolution (STRICT locking) before this test runs; this test catches a lock file rewritten with the
     * library in a shipped configuration.
     */
    @Test
    fun `SAT-PL-11 MockWebServer and okhttp-tls resolve only in test configurations`() {
        val results =
            sourceFiles()
                .filter { it.name == "gradle.lockfile" }
                .map { shippedTestLibraries(it.readText()) }
                .toList()
        assertTrue(results.sumOf { it.first } > 0, "no lock file lists the test-only libraries")
        assertEquals(emptyList<String>(), results.flatMap { it.second })
        val planted = "com.squareup.okhttp3:okhttp-tls:4.12.0=debugUnitTestRuntimeClasspath,releaseRuntimeClasspath"
        assertEquals(
            listOf("com.squareup.okhttp3:okhttp-tls:4.12.0 in releaseRuntimeClasspath"),
            shippedTestLibraries(planted).second,
        )
    }
}
