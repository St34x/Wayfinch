// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.sat

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.w3c.dom.Document

/**
 * SAT-PL-01, -02, -03, -04 (manifest part), -06 (manifest part), -07 (manifest part), -08, -09 over the merged
 * release manifest of :app (path from Gradle; the Test task takes the release MERGED_MANIFEST artifact as an input).
 * Each rule also runs on a planted negative: the real manifest with one change, which must fail it.
 */
class ManifestSecurityTest {
    private val text = mergedManifest()
    private val allowlist: Set<String> =
        checkNotNull(javaClass.getResource("/sat/permission-allowlist.txt"))
            .readText()
            .lines()
            .map { it.trim() }
            .filter { it.isNotEmpty() && !it.startsWith("#") }
            .toSet()

    /** compileSdk = targetSdk = minSdk of every build but a debug one with -PtestMinSdk (Android 17 only). */
    private val releaseSdk = 37

    private fun pass(rule: (Document) -> List<String>) = assertEquals(emptyList<String>(), rule(parseXml(text)))

    private fun fails(
        rule: (Document) -> List<String>,
        planted: String,
    ) = assertTrue(rule(parseXml(planted)).isNotEmpty(), "planted negative passed the rule")

    private fun withComponent(xml: String) = text.plant("</application>", "$xml</application>")

    private fun withAppAttribute(attr: String) = text.plant("<application", "<application $attr")

    @Test
    fun `SAT-PL-01 only MainActivity and EnrollActivity are exported`() {
        pass(ManifestRules::exported)
        fails(ManifestRules::exported, withComponent("<service android:name=\"x.Dummy\" android:exported=\"true\"/>"))
        fails(ManifestRules::exported, withComponent("<activity android:name=\"x.Extra\" android:exported=\"true\"/>"))
        fails(ManifestRules::exported, text.plant("android:scheme=\"wayfinch\"", "android:scheme=\"https\""))
        val withPath = text.plant("android:host=\"enroll\"", "android:host=\"enroll\" android:path=\"/x\"")
        fails(ManifestRules::exported, withPath)
    }

    @Test
    fun `SAT-PL-02 no other intent filter and no provider`() {
        pass(ManifestRules::filtersAndProviders)
        fails(
            ManifestRules::filtersAndProviders,
            withComponent("<provider android:name=\"x.P\" android:authorities=\"x.p\" android:exported=\"false\"/>"),
        )
        fails(
            ManifestRules::filtersAndProviders,
            withComponent(
                "<activity android:name=\"x.F\" android:exported=\"false\"><intent-filter>" +
                    "<action android:name=\"x.ACTION\"/></intent-filter></activity>",
            ),
        )
    }

    @Test
    fun `SAT-PL-03 empty task affinity and no reparenting`() {
        pass(ManifestRules::taskAffinity)
        fails(ManifestRules::taskAffinity, text.plant("android:taskAffinity=\"\"", "android:taskAffinity=\"x.y\""))
        fails(ManifestRules::taskAffinity, withAppAttribute("android:allowTaskReparenting=\"true\""))
    }

    @Test
    fun `SAT-PL-04 manifest references the network security config and never allows cleartext`() {
        pass(ManifestRules::networkConfig)
        fails(ManifestRules::networkConfig, withAppAttribute("android:usesCleartextTraffic=\"true\""))
        fails(ManifestRules::networkConfig, text.plant("@xml/network_security_config", "@xml/other_config"))
    }

    @Test
    fun `SAT-PL-06 backups off in the manifest`() {
        pass(ManifestRules::backup)
        fails(ManifestRules::backup, text.plant("android:allowBackup=\"false\"", "android:allowBackup=\"true\""))
        fails(ManifestRules::backup, text.plant("@xml/data_extraction_rules", "@xml/other_rules"))
    }

    @Test
    fun `SAT-PL-07 the release manifest is not debuggable`() {
        pass(ManifestRules::debuggable)
        fails(ManifestRules::debuggable, withAppAttribute("android:debuggable=\"true\""))
    }

    @Test
    fun `SAT-PL-07 the release manifest keeps minSdk and targetSdk 37`() {
        val rule = { doc: Document -> ManifestRules.sdkLevels(doc, releaseSdk) }
        pass(rule)
        fails(rule, text.plant("android:minSdkVersion=\"$releaseSdk\"", "android:minSdkVersion=\"36\""))
        fails(rule, text.plant("android:targetSdkVersion=\"$releaseSdk\"", "android:targetSdkVersion=\"36\""))
    }

    private fun missingClasses(doc: Document): List<String> =
        ManifestRules.classNames(doc).filter { name ->
            runCatching { Class.forName(name, false, javaClass.classLoader) }.isFailure
        }

    @Test
    fun `every class the merged manifest names exists, whatever appId is`() {
        val names = ManifestRules.classNames(parseXml(text))
        assertTrue("app.hudcompanion.shell.MainActivity" in names, "names: $names")
        assertTrue("app.hudcompanion.enroll.EnrollActivity" in names, "names: $names")
        pass(::missingClasses)
        fails(::missingClasses, withComponent("<activity android:name=\"x.Missing\" android:exported=\"false\"/>"))
    }

    @Test
    fun `SAT-PL-08 no direct boot and no device protected storage`() {
        pass(ManifestRules::directBoot)
        fails(ManifestRules::directBoot, withAppAttribute("android:defaultToDeviceProtectedStorage=\"true\""))
        val aware = withComponent("<service android:name=\"x.S\" android:directBootAware=\"true\"/>")
        fails(ManifestRules::directBoot, aware)
    }

    @Test
    fun `SAT-PL-09 permissions equal the allowlist file`() {
        assertEquals(emptyList<String>(), ManifestRules.permissions(parseXml(text), allowlist))
        val camera =
            text.plant("</manifest>", "<uses-permission android:name=\"android.permission.CAMERA\"/></manifest>")
        assertTrue(ManifestRules.permissions(parseXml(camera), allowlist).isNotEmpty())
        assertTrue(ManifestRules.permissions(parseXml(camera), allowlist + "android.permission.CAMERA").isNotEmpty())
        val listener =
            withComponent(
                "<service android:name=\"x.N\" android:exported=\"false\" " +
                    "android:permission=\"android.permission.BIND_NOTIFICATION_LISTENER_SERVICE\"/>",
            )
        assertTrue(ManifestRules.permissions(parseXml(listener), allowlist).isNotEmpty())
        val declared = text.plant("</manifest>", "<permission android:name=\"x.p.GUARD\"/></manifest>")
        assertTrue(ManifestRules.permissions(parseXml(declared), allowlist).isNotEmpty())
    }
}
