// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol

import app.hudcompanion.protocol.generated.decodeEnvelope
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable

/**
 * protocol/fixtures/lexical.json through the generated decoder (T1.105). The Python codec runs the same file
 * (test_envelope_lexical_cases.py): a bare value that is not JSON, a raw control character in a string, whitespace that
 * JSON does not have and nesting over 35 brackets are refused; the accepted cases decode. The nesting cases are built
 * here (35 open brackets pass, 36 do not, 100000 are refused without being parsed).
 */
class EnvelopeLexicalTest {
    private val cases = Json.parseToJsonElement(fixtureDir.resolve("lexical.json").readText()).jsonObject
    private val notJson = "not valid JSON"

    private val head = """{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","type":"%s","ts":"2026-10-03T18:00:00Z","body":%s}"""
    private val positions: Map<String, (String) -> String> =
        mapOf(
            "output" to { value -> result("""{"n":$value}""") },
            "item" to { value -> manifest("""{"name":"device.status","n":$value}""") },
            "args" to { value ->
                head
                    .format(
                        "skill.call",
                        """{"callId":"01JABCDEFGHJKMNPQRSTVWXYZ0","skill":"audio.volume_set","args":{"n":$value},""" +
                            """"mode":"act","deadlineMs":10000}""",
                    ).dropLast(1) + ""","seq":7}"""
            },
        )

    private fun result(output: String) =
        head.format("skill.result", """{"callId":"01JABCDEFGHJKMNPQRSTVWXYZ0","status":"ok","output":$output}""")

    private fun manifest(item: String) = head.format("skill.manifest", """{"manifestVersion":1,"skills":[$item]}""")

    private fun strings(
        group: String,
        key: String,
    ): List<String> =
        cases
            .getValue(group)
            .jsonObject
            .getValue(key)
            .jsonArray
            .map { it.jsonPrimitive.content }

    private fun points(
        group: String,
        key: String,
    ): List<Int> =
        cases
            .getValue(group)
            .jsonObject
            .getValue(key)
            .jsonArray
            .map { it.jsonPrimitive.int }

    private fun assertRefused(
        text: String,
        reason: String,
        label: String,
    ) {
        val failure = assertThrows(SerializationException::class.java, Executable { decodeEnvelope(text) }, label)
        assertTrue(failure.message.orEmpty().contains(reason), "$label: '$reason' not in '${failure.message}'")
    }

    private fun assertAccepted(
        text: String,
        label: String,
    ) {
        assertTrue(decodeEnvelope(text).body.frameType.isNotEmpty(), label)
    }

    @Test
    fun `a bare value that is not JSON is refused in every open object`() {
        for ((position, build) in positions) {
            for (value in strings("bareValues", "refused")) assertRefused(build(value), notJson, "$position $value")
        }
    }

    @Test
    fun `a bare JSON value in an open object is accepted and 1e-999 is not an overflow`() {
        for ((position, build) in positions) {
            for (value in strings("bareValues", "accepted")) assertAccepted(build(value), "$position $value")
        }
    }

    @Test
    fun `an integer literal has at most the digits of the limit, the sign not counted`() {
        val limit = cases.getValue("integerDigits").jsonPrimitive.int
        val build = positions.getValue("output")
        assertAccepted(build("1".repeat(limit)), "digits")
        assertAccepted(build("-" + "1".repeat(limit)), "negative digits")
        assertRefused(build("1".repeat(limit + 1)), notJson, "digits + 1")
        assertRefused(build("-" + "1".repeat(limit + 1)), notJson, "negative digits + 1")
    }

    @Test
    fun `an enum value without quotes is refused, also when the bare word is a JSON literal`() {
        val msgIn = { origin: String -> head.format("msg.in", """{"text":"hi","origin":$origin}""") }
        assertAccepted(msgIn("\"ptt\""), "quoted")
        for (origin in listOf("ptt", "true", "null", "1")) assertRefused(msgIn(origin), "", "enum $origin")
        assertRefused(msgIn("ptt"), notJson, "enum ptt")
    }

    private fun inText(char: Char) = head.format("msg.in", """{"text":"a${char}b","origin":"ptt"}""")

    private fun inKey(char: Char) = positions.getValue("output")("""1,"a${char}b":2""")

    @Test
    fun `a raw control character in a string or a key is refused, from space up it is accepted`() {
        for (build in listOf(::inText, ::inKey)) {
            for (point in points("stringCharacters", "refused")) {
                assertRefused(build(point.toChar()), notJson, "U+${point.toString(16)}")
            }
            for (point in points("stringCharacters", "accepted")) assertAccepted(build(point.toChar()), "U+$point")
        }
    }

    private fun withEscape(escape: String) = head.format("msg.in", "{\"text\":\"a${escape}b\",\"origin\":\"ptt\"}")

    @Test
    fun `an escape sequence is accepted when JSON has it and refused when it does not`() {
        for (escape in strings("escapes", "accepted")) assertAccepted(withEscape(escape), escape)
        for (escape in strings("escapes", "refused")) assertRefused(withEscape(escape), "", escape)
    }

    private fun spaced(char: Char) = result("""$char{"n":1}""")

    @Test
    fun `only the four whitespace characters of JSON separate tokens`() {
        for (point in points("whitespace", "refused")) assertRefused(spaced(point.toChar()), notJson, "U+$point")
        for (point in points("whitespace", "accepted")) assertAccepted(spaced(point.toChar()), "U+$point")
    }

    private fun nested(brackets: Int) =
        manifest("""{"name":"device.status","n":${"[".repeat(brackets - 4)}1${"]".repeat(brackets - 4)}}""")

    @Test
    fun `35 open brackets at once are accepted and 36 are refused, like the pre-scan of the Python codec`() {
        assertAccepted(nested(34), "34")
        assertAccepted(nested(35), "35")
        assertRefused(nested(36), "nesting deeper than 35", "36")
        assertRefused(nested(37), "nesting deeper than 35", "37")
    }

    @Test
    fun `brackets inside strings and closed brackets do not count towards the nesting`() {
        val inString = manifest("""{"name":"device.status","n":"${"[".repeat(100)}"}""")
        assertAccepted(inString, "brackets in a string")
        val siblings = manifest("""{"name":"device.status","n":${List(100) { "[1]" }.joinToString(",", "[", "]")}}""")
        assertAccepted(siblings, "100 closed arrays side by side")
    }

    @Test
    fun `100000 open brackets are refused at the 36th without parsing`() {
        val deep = "[".repeat(100_000)
        assertRefused(deep, "nesting deeper than 35", "100000 arrays")
        assertRefused("{\"a\":".repeat(100_000), "nesting deeper than 35", "100000 objects")
        val deepItem = nested(36).replace("\"n\":[", "\"n\":" + "[".repeat(100_000))
        assertRefused(deepItem, "nesting deeper than 35", "deep item")
    }
}
