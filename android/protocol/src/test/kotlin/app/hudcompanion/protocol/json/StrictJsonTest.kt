// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.json

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable

/** The [StrictJson] contract (T1.23a): each defect is refused alone, the object without it is read. */
class StrictJsonTest {
    private fun read(
        text: String,
        maxBytes: Int = 4096,
        maxDepth: Int = 8,
    ) = StrictJson.readObject(text, maxBytes, maxDepth)

    private fun assertRefused(
        rows: List<Pair<String, JsonRefusal>>,
        maxDepth: Int = 8,
    ) = assertAll(
        rows.map { (text, reason) ->
            Executable { assertEquals(StrictJsonResult.Refused(reason), read(text, maxDepth = maxDepth), text) }
        },
    )

    private fun assertRead(texts: List<String>) =
        assertAll(texts.map { text -> Executable { assertTrue(read(text) is StrictJsonResult.Read, text) } })

    @Test
    fun `a repeated key is refused at any depth and when spelled with a u escape`() {
        assertRefused(
            listOf(
                """{"a":1,"a":2}""" to JsonRefusal.DUPLICATE_KEY,
                """{"x":{"y":{"a":1,"a":2}}}""" to JsonRefusal.DUPLICATE_KEY,
                """{"x":[{"a":1,"a":2}]}""" to JsonRefusal.DUPLICATE_KEY,
                """{"a":1,"\u0061":2}""" to JsonRefusal.DUPLICATE_KEY,
                """{"\u0061":1,"a":2}""" to JsonRefusal.DUPLICATE_KEY,
            ),
        )
    }

    @Test
    fun `the same objects without the repeated key are read`() {
        assertRead(
            listOf(
                """{"a":1,"b":2}""",
                """{"x":{"y":{"a":1,"b":2}}}""",
                """{"x":[{"a":1},{"a":2}]}""",
                """{"a":1,"\u0062":2}""",
                """{"a":{"a":1}}""",
            ),
        )
    }

    @Test
    fun `trailing bytes, a BOM, NaN and a number beyond a finite double are refused`() {
        assertRefused(
            listOf(
                """{"a":1} x""" to JsonRefusal.TRAILING,
                """{"a":1}{}""" to JsonRefusal.TRAILING,
                "\uFEFF{\"a\":1}" to JsonRefusal.BOM,
                """{"a":NaN}""" to JsonRefusal.SYNTAX,
                """{"a":Infinity}""" to JsonRefusal.SYNTAX,
                """{"a":-Infinity}""" to JsonRefusal.SYNTAX,
                """{"a":1e999}""" to JsonRefusal.NON_FINITE_NUMBER,
                """{"a":-1e999}""" to JsonRefusal.NON_FINITE_NUMBER,
                "{\"a\":1${"0".repeat(400)}}" to JsonRefusal.NON_FINITE_NUMBER,
            ),
        )
    }

    @Test
    fun `the same objects without those defects are read`() {
        assertRead(listOf("""{"a":1} """, "\n{\"a\":1}\r\n", """{"a":1e300}""", """{"a":-0.5E-3}""", "{}"))
    }

    @Test
    fun `text that is not one JSON object is refused`() {
        assertRefused(
            listOf(
                "" to JsonRefusal.SYNTAX,
                "   " to JsonRefusal.SYNTAX,
                "[1]" to JsonRefusal.NOT_AN_OBJECT,
                "1" to JsonRefusal.NOT_AN_OBJECT,
                "\"a\"" to JsonRefusal.NOT_AN_OBJECT,
                """{"a":1""" to JsonRefusal.SYNTAX,
                """{"a":1,}""" to JsonRefusal.SYNTAX,
                """{"a" 1}""" to JsonRefusal.SYNTAX,
                """{a:1}""" to JsonRefusal.SYNTAX,
                """{'a':1}""" to JsonRefusal.SYNTAX,
                """{"a":[1,]}""" to JsonRefusal.SYNTAX,
                """{"a":[,1]}""" to JsonRefusal.SYNTAX,
                """{"a":01}""" to JsonRefusal.SYNTAX,
                """{"a":+1}""" to JsonRefusal.SYNTAX,
                """{"a":1.}""" to JsonRefusal.SYNTAX,
                """{"a":.5}""" to JsonRefusal.SYNTAX,
                """{"a":tru}""" to JsonRefusal.SYNTAX,
                """{"a":"\x"}""" to JsonRefusal.SYNTAX,
                """{"a":"\u00g1"}""" to JsonRefusal.SYNTAX,
                """{"a":"\u00"}""" to JsonRefusal.SYNTAX,
                "{\"a\":\"x\ty\"}" to JsonRefusal.SYNTAX,
                "{\"a\":\"x\u0000\"}" to JsonRefusal.SYNTAX,
                "{\"a\":1}\u00A0" to JsonRefusal.TRAILING,
                "\u00A0{\"a\":1}" to JsonRefusal.SYNTAX,
            ),
        )
    }

    @Test
    fun `nesting deeper than the bound is refused without reading it`() {
        assertRefused(listOf("""{"a":{"b":[1]}}""" to JsonRefusal.TOO_DEEP), maxDepth = 2)
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_DEEP), read("{\"a\":" + "[".repeat(100_000), 1_000_000))
        assertTrue(read("""{"a":{"b":1}}""", maxDepth = 2) is StrictJsonResult.Read)
        assertTrue(read("""{"a":[1],"b":[2],"c":{"d":1},"e":[[]]}""", maxDepth = 3) is StrictJsonResult.Read)
        assertTrue(read("""{"a":[1],"b":[2],"c":{"d":1}}""", maxDepth = 2) is StrictJsonResult.Read)
    }

    @Test
    fun `an unpaired surrogate is read as it is, so callers shape-check every string they use`() {
        val value = (read("{\"a\":\"\\uD800\",\"b\":\"x\uDC00\"}") as StrictJsonResult.Read).value
        assertEquals("\uD800", value.getValue("a").jsonPrimitive.content)
        assertEquals("x\uDC00", value.getValue("b").jsonPrimitive.content)
    }

    @Test
    fun `the byte bound counts UTF-8 bytes`() {
        val text = "{\"a\":\"${"é".repeat(10)}\"}"
        assertEquals(28, StrictJson.utf8Length(text, 100))
        assertTrue(read(text, maxBytes = 28) is StrictJsonResult.Read)
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_LARGE), read(text, maxBytes = 27))
        assertEquals(4, StrictJson.utf8Length("\uD83D\uDE00", 10))
        assertEquals(3, StrictJson.utf8Length("\uD83D", 10))
    }

    @Test
    fun `values come back unescaped with their JSON types`() {
        val text = """{"s":"\u00e9\n\"\\\/","n":-12,"d":2.5,"t":true,"z":null,"l":[1,"x"]}"""
        val value = (read(text) as StrictJsonResult.Read).value
        assertEquals("é\n\"\\/", value.getValue("s").jsonPrimitive.content)
        assertEquals(JsonPrimitive(-12L), value.getValue("n"))
        assertEquals(JsonPrimitive(2.5), value.getValue("d"))
        assertEquals("true", value.getValue("t").jsonPrimitive.content)
        assertEquals("null", value.getValue("z").jsonPrimitive.content)
        assertEquals(2, value.getValue("l").jsonArray.size)
        assertEquals(listOf("s", "n", "d", "t", "z", "l"), value.jsonObject.keys.toList())
    }
}
