// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.json

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import kotlin.random.Random

/** Malformed, truncated, oversized and deeply nested texts for [StrictJson]: one refusal each, never an exception. */
class StrictJsonEdgeCasesTest {
    private fun read(
        text: String,
        maxBytes: Int = BIG,
        maxDepth: Int = 4,
    ): StrictJsonResult = StrictJson.readObject(text, maxBytes, maxDepth)

    private fun assertRefused(
        reason: JsonRefusal,
        texts: List<String>,
    ) = assertAll(
        texts.map { text -> Executable { assertEquals(StrictJsonResult.Refused(reason), read(text), text) } },
    )

    private fun readValue(text: String): JsonObject =
        (read(text) as? StrictJsonResult.Read ?: throw AssertionError("refused: $text")).value

    @Test
    fun `malformed grammar is a syntax refusal`() {
        assertRefused(
            JsonRefusal.SYNTAX,
            listOf(
                "",
                " ",
                "{",
                "{\"a\"",
                "{\"a\":",
                "{\"a\":1",
                "{\"a\":1,}",
                "{,}",
                "{\"a\":1,,\"b\":2}",
                "{a:1}",
                "{'a':1}",
                "{\"a\":'x'}",
                "{\"a\":1 \"b\":2}",
                "{\"a\"\"b\"}",
                "{\"a\":[1,]}",
                "{\"a\":[,1]}",
                "{\"a\":[1 2]}",
                "{\"a\":\"unterminated}",
                "{\"a\":\u000C1}",
                "\u00A0{}",
                "\u200B{}",
            ),
        )
    }

    @Test
    fun `malformed numbers and literals are a syntax refusal`() {
        val values =
            listOf(
                "01",
                "+1",
                ".5",
                "1.",
                "-",
                "1e",
                "1e+",
                "0x10",
                "tru",
                "True",
                "nul",
                "NULL",
                "NaN",
                "Infinity",
                "-Infinity",
                "-NaN",
            )
        assertRefused(JsonRefusal.SYNTAX, values.map { "{\"a\":$it}" })
    }

    @Test
    fun `malformed strings and escapes are a syntax refusal`() {
        assertRefused(
            JsonRefusal.SYNTAX,
            listOf(
                """{"a":"\x"}""",
                """{"a":"\u12"}""",
                """{"a":"\u12G4"}""",
                """{"a":"\u+123"}""",
                """{"a":"\"}""",
                """{"a":"\U0041"}""",
                "{\"a\":\"line\nbreak\"}",
                "{\"a\":\"tab\there\"}",
                "{\"a\":\"nul\u0000\"}",
                "{\"a\":\"unit\u001Fsep\"}",
            ),
        )
    }

    @Test
    fun `a complete object followed by anything but whitespace is a trailing refusal`() {
        assertRefused(
            JsonRefusal.TRAILING,
            listOf("{}{}", "{} {}", "{}x", "{}\u0000", "{}\u000C", "{}\u00A0", "{}}", "{};", "{},"),
        )
    }

    @Test
    fun `a text that is one JSON value but not an object is refused`() {
        assertRefused(JsonRefusal.NOT_AN_OBJECT, listOf("[]", "[{}]", "1", "\"a\"", "null", "true", "-0.5"))
    }

    @Test
    fun `a number beyond a finite double is refused wherever it stands`() {
        val values = listOf("1e999", "-1e999", "1E400", "1e99999999999999999999", "9".repeat(400))
        assertRefused(JsonRefusal.NON_FINITE_NUMBER, values.flatMap { listOf("{\"a\":$it}", "{\"a\":[$it]}") })
    }

    @Test
    fun `numbers that fit a finite double are read, an underflow becomes zero`() {
        val values = listOf("-0", "0.0", "1e-999", "12345678901234567890123", "-1.5E+3", "9223372036854775807")
        values.forEach { assertTrue(read("{\"a\":$it}") is StrictJsonResult.Read, it) }
        assertEquals(JsonPrimitive(Long.MAX_VALUE), readValue("{\"a\":9223372036854775807}")["a"])
    }

    @Test
    fun `nesting far deeper than the bound is refused without recursing that deep`() {
        val deepObjects = "{\"a\":".repeat(DEEP) + "1" + "}".repeat(DEEP)
        val deepArrays = "[".repeat(DEEP_UNTERMINATED)
        assertRefused(JsonRefusal.TOO_DEEP, listOf(deepObjects, "{\"a\":$deepArrays", "{\"a\":[[[[[1]]]]]}"))
    }

    @Test
    fun `the depth bound counts the outer object and is checked at the bound itself`() {
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_DEEP), read("{}", maxDepth = 0))
        assertEquals(StrictJsonResult.Read(JsonObject(emptyMap())), read("{}", maxDepth = 1))
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_DEEP), read("{\"a\":{}}", maxDepth = 1))
        assertTrue(read("{\"a\":{}}", maxDepth = 2) is StrictJsonResult.Read)
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_DEEP), read("{\"a\":[[1]]}", maxDepth = 2))
        assertTrue(read("{\"a\":[[1]]}", maxDepth = 3) is StrictJsonResult.Read)
    }

    @Test
    fun `the byte bound is exact and a huge text is refused before it is read`() {
        assertTrue(read("{}", maxBytes = 2) is StrictJsonResult.Read)
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_LARGE), read("{}", maxBytes = 1))
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_LARGE), read("{}", maxBytes = 0))
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_LARGE), read("{\"a\":\"${"x".repeat(HUGE)}\"}", 1024))
        assertEquals(StrictJsonResult.Refused(JsonRefusal.TOO_LARGE), read("[".repeat(HUGE), 1024))
    }

    @Test
    fun `a repeated key is refused however it is spelled or placed`() {
        assertRefused(
            JsonRefusal.DUPLICATE_KEY,
            listOf(
                """{"a":1,"a":1}""",
                """{"a":1,"a":"x"}""",
                """{"a":null,"a":1}""",
                """{"a":[],"a":[]}""",
                """{"":1,"":2}""",
                """{"a":{"a":1},"a":2}""",
                """{"x":[{"k":1,"k":2}]}""",
                """{"J":1,"\u004a":2}""",
                """{"J":1,"\u004A":2}""",
                """{"a/b":1,"a\/b":2}""",
                """{"\u0061":1,"\u0061":2}""",
                "{\"\uD83D\uDE00\":1,\"\\uD83D\\uDE00\":2}",
                """{"line\nbreak":1,"line\u000abreak":2}""",
            ),
        )
        assertEquals(setOf("a", "A"), readValue("""{"a":1,"A":2}""").keys)
        assertEquals(setOf("a", "a "), readValue("""{"a":1,"a ":2}""").keys)
    }

    @Test
    fun `no prefix of a valid text is read`() {
        val text = """{"a":[1,2.5,"x\u00e9",true,null,{"b":{}}],"c":"d"}"""
        assertTrue(read(text) is StrictJsonResult.Read)
        val prefixes = (0 until text.length).map { text.substring(0, it) }
        prefixes.forEach { assertTrue(read(it) is StrictJsonResult.Refused, it) }
    }

    @Test
    fun `random garbage never throws and anything read is text kotlinx also reads`() {
        val random = Random(SEED)
        val alphabet = "{}[]\":,\\ \n\t0123456789.eE+-tfnrulsaAxu/\u00e9\uD800"
        repeat(RANDOM_TEXTS) {
            val text = String(CharArray(random.nextInt(0, MAX_RANDOM)) { alphabet[random.nextInt(alphabet.length)] })
            val result = read(text)
            if (result is StrictJsonResult.Read) Json.parseToJsonElement(text)
        }
    }

    @Test
    fun `whitespace between any two tokens other than before the name separator is read`() {
        val random = Random(SEED)
        repeat(RANDOM_DOCS) { assertDocumentRead(random, beforeColon = false) }
    }

    @Test
    fun `whitespace before the name separator is read`() {
        assertEquals(setOf("a"), readValue("{\"a\" :1}").keys)
        val random = Random(SEED)
        repeat(RANDOM_DOCS) { assertDocumentRead(random, beforeColon = true) }
    }

    private fun assertDocumentRead(
        random: Random,
        beforeColon: Boolean,
    ) {
        fun space() = if (random.nextBoolean()) "" else " \t\r\n"[random.nextInt(4)].toString()

        val members = mutableMapOf<String, JsonPrimitive>()
        repeat(random.nextInt(1, 5)) {
            members["k${random.nextInt(1000)}"] =
                when (random.nextInt(4)) {
                    0 -> JsonPrimitive(random.nextLong(-1000, 1000))
                    1 -> JsonPrimitive("v${random.nextInt(1000)}")
                    2 -> JsonPrimitive(random.nextBoolean())
                    else -> JsonNull
                }
        }
        val colon = (if (beforeColon) space() else "") + ":" + space()
        val body = members.entries.joinToString(space() + "," + space()) { "\"${it.key}\"$colon${it.value}" }
        val text = space() + "{" + space() + body + space() + "}" + space()
        assertEquals(JsonObject(members), readValue(text), text)
    }

    private companion object {
        const val BIG = 1 shl 22
        const val DEEP = 5_000
        const val DEEP_UNTERMINATED = 1_000_000
        const val HUGE = 3_000_000
        const val SEED = 20261023
        const val RANDOM_TEXTS = 4_000
        const val RANDOM_DOCS = 300
        const val MAX_RANDOM = 40
    }
}
