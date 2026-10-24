// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.sat

import org.w3c.dom.Document
import org.w3c.dom.Element
import org.xml.sax.InputSource
import java.io.File
import java.io.StringReader
import javax.xml.parsers.DocumentBuilderFactory

/** Shared readers of the security acceptance tests (SAT-PL-01..11): XML parsing, files passed in by Gradle. */
internal const val ANDROID_NS = "http://schemas.android.com/apk/res/android"

internal fun parseXml(text: String): Document {
    val factory = DocumentBuilderFactory.newInstance()
    factory.isNamespaceAware = true
    factory.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true)
    return factory.newDocumentBuilder().parse(InputSource(StringReader(text)))
}

internal fun Element.android(name: String): String? =
    if (hasAttributeNS(ANDROID_NS, name)) getAttributeNS(ANDROID_NS, name) else null

internal fun Element.children(tag: String? = null): List<Element> {
    val nodes = childNodes
    return (0 until nodes.length).mapNotNull { nodes.item(it) as? Element }.filter { tag == null || it.tagName == tag }
}

internal fun Document.all(tag: String): List<Element> {
    val nodes = getElementsByTagName(tag)
    return (0 until nodes.length).map { nodes.item(it) as Element }
}

internal fun propertyFile(name: String): File {
    val path = System.getProperty(name) ?: error("system property $name is not set: run the tests through Gradle")
    return File(path).also { check(it.exists()) { "$name points to a missing path: $it" } }
}

internal val androidRoot: File get() = propertyFile("sat.androidRoot")

internal fun mergedManifest(): String = propertyFile("sat.mergedManifest").readText()

internal fun appXml(name: String): String = File(androidRoot, "app/src/main/res/xml/$name").readText()

/** Every file under android/ outside build output and Gradle caches. */
internal fun sourceFiles(): Sequence<File> =
    androidRoot
        .walkTopDown()
        .onEnter { it.name != "build" && it.name != ".gradle" && it.name != ".kotlin" }
        .filter { it.isFile }

/** Replaces exactly one occurrence; a planted negative that matches nothing would test nothing. */
internal fun String.plant(
    old: String,
    new: String,
): String {
    check(split(old).size == 2) { "planted negative needs exactly one '$old'" }
    return replace(old, new)
}
