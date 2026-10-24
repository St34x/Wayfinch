// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol

import kotlinx.serialization.KSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.serializer
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import java.io.File

/**
 * Every multi-valued `enum` of the definitions of protocol/envelope.schema.json is a generated enum class that
 * carries its wire values, in order, and every generated enum class is such an enum. The classes are the ones declared
 * in Envelope.kt (read from the generator's output), so this test holds no second copy of the generator's naming rule;
 * the value lists are compared by content. An enum added to the schema is checked without editing this test.
 */
class EnvelopeEnumsTest {
    private val schema = File(requireNotNull(System.getProperty("protocol.schema")) { "protocol.schema unset" })
    private val generated = File("src/main/kotlin/app/hudcompanion/protocol/generated/Envelope.kt")

    private fun collect(
        spec: JsonObject,
        into: MutableSet<List<String>>,
    ) {
        val values = spec["enum"]?.jsonArray?.map { it.jsonPrimitive.content }
        if (values != null && values.size > 1) into.add(values)
        spec["items"]?.jsonObject?.let { collect(it, into) }
        spec["properties"]?.jsonObject?.values?.forEach { child ->
            if (child.jsonObject["\$ref"] == null) collect(child.jsonObject, into)
        }
    }

    private fun wireValues(serializer: KSerializer<*>): List<String> =
        (0 until serializer.descriptor.elementsCount).map { serializer.descriptor.getElementName(it) }

    @Test
    fun `the enum classes of Envelope kt carry exactly the enums of the schema, values in order`() {
        val fromSchema = mutableSetOf<List<String>>()
        val defs =
            Json
                .parseToJsonElement(schema.readText())
                .jsonObject
                .getValue("\$defs")
                .jsonObject
        defs.values.forEach { collect(it.jsonObject, fromSchema) }
        val declaration = Regex("""public enum class (\w+)""")
        val names = declaration.findAll(generated.readText()).map { it.groupValues[1] }.toList()
        assertTrue(names.size >= 16, "enum classes found in ${generated.path}: $names")
        val fromCode =
            names.map { name ->
                val type = Class.forName("app.hudcompanion.protocol.generated.$name")
                assertTrue(type.isEnum, "$name is not an enum class")
                wireValues(serializer(type))
            }
        assertEquals(fromSchema.map { it.toString() }.sorted(), fromCode.toSet().map { it.toString() }.sorted())
    }
}
