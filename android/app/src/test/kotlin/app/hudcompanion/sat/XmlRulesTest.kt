// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.sat

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.w3c.dom.Document
import org.w3c.dom.Element
import java.io.File
import java.nio.file.Files

/** SAT-PL-04, -05 (network security config) and SAT-PL-06 (backup rule files), parsed, never grepped. */
class XmlRulesTest {
    private val nsc = appXml("network_security_config.xml")
    private val extraction = appXml("data_extraction_rules.xml")
    private val fullBackup = appXml("backup_rules.xml")

    /** Every backup domain: credential-encrypted storage, external storage and device-protected (direct boot). */
    private val domains =
        setOf(
            "root",
            "file",
            "database",
            "sharedpref",
            "external",
            "device_root",
            "device_file",
            "device_database",
            "device_sharedpref",
        )

    private fun nscRules(doc: Document): List<String> {
        val root = doc.documentElement
        val bad = mutableListOf<String>()
        if (root.tagName != "network-security-config") bad += "root is ${root.tagName}"
        if (root.children().map { it.tagName } != listOf("base-config")) bad += "root children are not one base-config"
        val config = root.children("base-config").firstOrNull() ?: return bad + "no base-config"
        if (config.getAttribute("cleartextTrafficPermitted") != "false") bad += "cleartext is not false"
        val inner = config.children()
        val innerTags = inner.map { it.tagName }
        if (innerTags != listOf("domainEncryption")) bad += "base-config children: $innerTags"
        if (inner.firstOrNull()?.getAttribute("mode") != "disabled") bad += "domainEncryption is not disabled"
        for (tag in listOf("trust-anchors", "certificates", "pin-set", "domain-config", "debug-overrides")) {
            if (doc.all(tag).isNotEmpty()) bad += "<$tag> present"
        }
        return bad
    }

    @Test
    fun `SAT-PL-04 one base-config, no cleartext, domain encryption disabled, system anchors only`() {
        assertEquals(emptyList<String>(), nscRules(parseXml(nsc)))
        val anchors = "<trust-anchors><certificates src=\"user\"/></trust-anchors>"
        val plants =
            listOf(
                nsc.plant("<domainEncryption mode=\"disabled\" />", "<domainEncryption mode=\"disabled\" />$anchors"),
                nsc.plant("</base-config>", "</base-config><debug-overrides>$anchors</debug-overrides>"),
                nsc.plant("</base-config>", "</base-config><domain-config><domain>x.example</domain></domain-config>"),
                nsc.plant("cleartextTrafficPermitted=\"false\"", "cleartextTrafficPermitted=\"true\""),
                nsc.plant("mode=\"disabled\"", "mode=\"enabled\""),
            )
        plants.forEach { assertTrue(nscRules(parseXml(it)).isNotEmpty(), "planted negative passed: $it") }
    }

    private fun isNsc(file: File): Boolean =
        runCatching { parseXml(file.readText()).documentElement.tagName }.getOrNull() == "network-security-config"

    /** Files under [root] (in res/xml* dirs) whose root element is a network security config, except app/src/main's. */
    private fun extraConfigs(root: File): List<String> =
        root
            .walkTopDown()
            .onEnter { it.name != "build" && it.name != ".gradle" }
            .filter { it.isFile && it.extension == "xml" && it.parentFile?.name?.startsWith("xml") == true }
            .filter { isNsc(it) }
            .map { it.relativeTo(root).invariantSeparatorsPath }
            .filter { it != "app/src/main/res/xml/network_security_config.xml" }
            .toList()

    @Test
    fun `SAT-PL-05 debug and release resolve the same network security config`() {
        assertEquals(emptyList<String>(), extraConfigs(androidRoot))
        val scratch = Files.createTempDirectory("sat-nsc").toFile()
        try {
            val paths = listOf("app/src/main/res/xml/network_security_config.xml", "app/src/debug/res/xml-v37/nsc.xml")
            for (path in paths) {
                val file = File(scratch, path)
                checkNotNull(file.parentFile).mkdirs()
                file.writeText(nsc)
            }
            assertEquals(listOf("app/src/debug/res/xml-v37/nsc.xml"), extraConfigs(scratch))
        } finally {
            scratch.deleteRecursively()
        }
    }

    private fun excludesAll(section: Element?): List<String> {
        if (section == null) return listOf("section missing")
        val excluded =
            section
                .children("exclude")
                .filter { it.getAttribute("path") == "." }
                .map { it.getAttribute("domain") }
                .toSet()
        return section.children("include").map { "include of ${it.getAttribute("domain")}" } +
            (domains - excluded).map { "${section.tagName} does not exclude $it" }
    }

    private fun backupRules(
        extractionXml: String,
        fullXml: String,
    ): List<String> {
        val ext = parseXml(extractionXml).documentElement
        val full = parseXml(fullXml).documentElement
        val sections = ext.children().map { it.tagName }
        val shape =
            listOfNotNull(
                "sections are $sections".takeIf { sections != listOf("cloud-backup", "device-transfer") },
                "root is ${ext.tagName}".takeIf { ext.tagName != "data-extraction-rules" },
            )
        return shape + excludesAll(ext.children("cloud-backup").singleOrNull()) +
            excludesAll(ext.children("device-transfer").singleOrNull()) +
            excludesAll(full.takeIf { it.tagName == "full-backup-content" })
    }

    private fun exclude(domain: String) = "<exclude domain=\"$domain\" path=\".\" />"

    @Test
    fun `SAT-PL-06 cloud backup, device transfer and full backup include nothing`() {
        assertEquals(emptyList<String>(), backupRules(extraction, fullBackup))
        val withoutFirstSharedpref = extraction.replaceFirst(exclude("sharedpref"), "")
        check(withoutFirstSharedpref != extraction)
        val withoutLastDeviceFile =
            extraction.substringBeforeLast(exclude("device_file")) +
                extraction.substringAfterLast(exclude("device_file"))
        check(withoutLastDeviceFile != extraction)
        val renamedTransfer =
            extraction
                .plant("<device-transfer>", "<device-transfer-x>")
                .plant("</device-transfer>", "</device-transfer-x>")
        val include = "<include domain=\"file\" path=\".\" />"
        val withInclude = extraction.plant("<device-transfer>", "<device-transfer>$include")
        val plants =
            listOf(
                withInclude to fullBackup,
                withoutFirstSharedpref to fullBackup,
                withoutLastDeviceFile to fullBackup,
                renamedTransfer to fullBackup,
                extraction to fullBackup.plant(exclude("database"), ""),
                extraction to fullBackup.plant(exclude("device_sharedpref"), ""),
            )
        plants.forEach { (e, f) -> assertTrue(backupRules(e, f).isNotEmpty(), "planted negative passed") }
    }
}
