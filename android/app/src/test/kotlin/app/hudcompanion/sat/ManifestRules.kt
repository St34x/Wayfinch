// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.sat

import org.w3c.dom.Document
import org.w3c.dom.Element

/**
 * Checks over the merged release manifest of :app. Each returns its violations (empty = pass) so the tests can run it
 * on the real manifest and on planted negatives.
 */
internal object ManifestRules {
    private val COMPONENTS = listOf("activity", "activity-alias", "service", "receiver", "provider")
    private val DECLARATIONS = listOf("permission", "permission-tree", "permission-group")
    private const val MAIN = "android.intent.action.MAIN"
    private const val LAUNCHER = "android.intent.category.LAUNCHER"
    private const val VIEW = "android.intent.action.VIEW"
    private val ENROLL_CATEGORIES = setOf("android.intent.category.DEFAULT", "android.intent.category.BROWSABLE")

    /** Never allowed, whatever the allowlist file says (SAT-PL-09). */
    val DENIED_PERMISSIONS =
        setOf(
            "android.permission.CAMERA",
            "android.permission.SYSTEM_ALERT_WINDOW",
            "android.permission.QUERY_ALL_PACKAGES",
            "android.permission.READ_EXTERNAL_STORAGE",
            "android.permission.WRITE_EXTERNAL_STORAGE",
            "android.permission.MANAGE_EXTERNAL_STORAGE",
            "android.permission.READ_MEDIA_IMAGES",
            "android.permission.READ_MEDIA_VIDEO",
            "android.permission.READ_MEDIA_AUDIO",
            "android.permission.READ_LOGS",
            "android.permission.REQUEST_INSTALL_PACKAGES",
            "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE",
            "android.permission.BIND_ACCESSIBILITY_SERVICE",
        )
    private val DENIED_BINDS =
        setOf(
            "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE",
            "android.permission.BIND_ACCESSIBILITY_SERVICE",
            "android.permission.BIND_DREAM_SERVICE",
        )
    private val DENIED_ACTIONS =
        setOf(
            "android.service.notification.NotificationListenerService",
            "android.service.dreams.DreamService",
            "android.accessibilityservice.AccessibilityService",
        )

    private fun Document.application(): Element = all("application").single()

    private fun Document.components(): List<Element> = COMPONENTS.flatMap { all(it) }

    private fun Element.name(): String = android("name") ?: "<no name>"

    private fun Element.filters(): List<Element> = children("intent-filter")

    private fun Element.values(tag: String): List<String> = children(tag).map { it.android("name").orEmpty() }

    private fun Element.isExported(): Boolean =
        android("exported") == "true" || (android("exported") == null && filters().isNotEmpty())

    private fun isLauncher(e: Element): Boolean {
        val f = e.filters().singleOrNull() ?: return false
        return e.tagName == "activity" && e.name().endsWith(".MainActivity") && f.values("action") == listOf(MAIN) &&
            f.values("category") == listOf(LAUNCHER) && f.children("data").isEmpty()
    }

    private fun isEnrollLink(e: Element): Boolean {
        val f = e.filters().singleOrNull()
        val data = f?.children("data")?.singleOrNull()
        return f != null && data != null && e.tagName == "activity" &&
            e.name().endsWith(".enroll.EnrollActivity") && hasEnrollFilter(f) && hasEnrollData(data)
    }

    private fun hasEnrollFilter(f: Element): Boolean =
        f.values("action") == listOf(VIEW) &&
            f.values("category").toSet() == ENROLL_CATEGORIES && f.values("category").size == 2 &&
            !f.hasAttributeNS(ANDROID_NS, "autoVerify")

    private fun hasEnrollData(data: Element): Boolean {
        val attrs = (0 until data.attributes.length).map { data.attributes.item(it).localName }.toSet()
        return attrs == setOf("scheme", "host") && data.android("scheme") == "wayfinch" &&
            data.android("host") == "enroll"
    }

    /** SAT-PL-01: exactly MainActivity (launcher) and EnrollActivity (wayfinch://enroll) are exported. */
    fun exported(doc: Document): List<String> {
        val exported = doc.components().filter { it.isExported() }
        val bad =
            exported
                .filterNot { isLauncher(it) || isEnrollLink(it) }
                .map { "exported ${it.tagName} ${it.name()}" }
        val launchers = exported.count { isLauncher(it) }
        val enroll = exported.count { isEnrollLink(it) }
        val missing =
            listOfNotNull(
                "MainActivity (MAIN/LAUNCHER) is not exported exactly once".takeIf { launchers != 1 },
                "EnrollActivity (wayfinch://enroll) is not exported exactly once".takeIf { enroll != 1 },
            )
        return bad + missing
    }

    /** SAT-PL-02: no other activity has an intent filter; no provider at all. */
    fun filtersAndProviders(doc: Document): List<String> {
        val activities = doc.all("activity") + doc.all("activity-alias")
        val filtered = activities.filter { it.filters().isNotEmpty() && !isLauncher(it) && !isEnrollLink(it) }
        return filtered.map { "intent filter on ${it.name()}" } + doc.all("provider").map { "provider ${it.name()}" }
    }

    /** SAT-PL-03: empty task affinity on the application; no task reparenting anywhere. */
    fun taskAffinity(doc: Document): List<String> {
        val app = doc.application()
        val affinity =
            listOfNotNull(
                "application taskAffinity is not \"\"".takeIf { app.android("taskAffinity") != "" },
            )
        val reparent = (listOf(app) + doc.components()).filter { it.android("allowTaskReparenting") == "true" }
        return affinity + reparent.map { "allowTaskReparenting on ${it.name()}" }
    }

    /** SAT-PL-04 (manifest part): the config is referenced; cleartext is never switched on. */
    fun networkConfig(doc: Document): List<String> {
        val app = doc.application()
        return listOfNotNull(
            "networkSecurityConfig is not @xml/network_security_config"
                .takeIf { app.android("networkSecurityConfig") != "@xml/network_security_config" },
            "usesCleartextTraffic is true".takeIf { app.android("usesCleartextTraffic") == "true" },
        )
    }

    /** SAT-PL-06 (manifest part): backups off and both rule files referenced. */
    fun backup(doc: Document): List<String> {
        val app = doc.application()
        return listOfNotNull(
            "allowBackup is not false".takeIf { app.android("allowBackup") != "false" },
            "dataExtractionRules is not @xml/data_extraction_rules"
                .takeIf { app.android("dataExtractionRules") != "@xml/data_extraction_rules" },
            "fullBackupContent is not @xml/backup_rules"
                .takeIf { app.android("fullBackupContent") != "@xml/backup_rules" },
            "backupAgent is set".takeIf { app.android("backupAgent") != null },
        )
    }

    /** SAT-PL-07 (manifest part): the release manifest is not debuggable. */
    fun debuggable(doc: Document): List<String> =
        listOfNotNull("application is debuggable".takeIf { doc.application().android("debuggable") == "true" })

    /**
     * SAT-PL-07 (minSdk part): the release manifest keeps minSdk and targetSdk [sdk]; the test-only override
     * (`-PtestMinSdk`) reaches the debug build type only.
     */
    fun sdkLevels(
        doc: Document,
        sdk: Int,
    ): List<String> {
        val usesSdk = doc.all("uses-sdk").singleOrNull() ?: return listOf("no single <uses-sdk>")
        return listOf("minSdkVersion", "targetSdkVersion")
            .filter { usesSdk.android(it) != "$sdk" }
            .map { "$it is ${usesSdk.android(it)}, not $sdk" }
    }

    /** Class names of the application and of every component except aliases, for the check that each one exists. */
    fun classNames(doc: Document): List<String> =
        listOf(doc.application()).mapNotNull { it.android("name") } +
            doc.components().filter { it.tagName != "activity-alias" }.map { it.name() }

    /** SAT-PL-08: nothing runs before the first unlock or keeps data in device-protected storage. */
    fun directBoot(doc: Document): List<String> =
        (listOf(doc.application()) + doc.components()).flatMap { e ->
            listOf("directBootAware", "defaultToDeviceProtectedStorage")
                .filter { e.android(it) == "true" }
                .map { "$it on ${e.tagName} ${e.name()}" }
        }

    /**
     * SAT-PL-09: the requested permissions equal the allowlist; the app declares no permission of its own (a library
     * that adds a `<permission>` fails); denied permissions, binds and services are absent.
     */
    fun permissions(
        doc: Document,
        allowlist: Set<String>,
    ): List<String> {
        val requested = (doc.all("uses-permission") + doc.all("uses-permission-sdk-23")).map { it.name() }.toSet()
        val declared = DECLARATIONS.flatMap { tag -> doc.all(tag).map { "declared $tag ${it.name()}" } }
        val services = doc.all("service")
        return (requested - allowlist).map { "permission not on the allowlist: $it" } +
            (allowlist - requested).map { "allowlisted permission missing: $it" } + declared +
            (requested + allowlist).filter { it in DENIED_PERMISSIONS }.map { "denied permission: $it" } +
            services.filter { it.android("permission") in DENIED_BINDS }.map { "denied bind on ${it.name()}" } +
            services
                .filter { s -> s.filters().any { f -> f.values("action").any { it in DENIED_ACTIONS } } }
                .map { "denied service ${it.name()}" }
    }
}
