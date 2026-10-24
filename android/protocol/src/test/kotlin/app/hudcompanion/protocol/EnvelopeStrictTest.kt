// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol

import app.hudcompanion.protocol.generated.AckBody
import app.hudcompanion.protocol.generated.decodeEnvelope
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.MissingFieldException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertInstanceOf
import org.junit.jupiter.api.Assertions.assertThrows
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable

/**
 * The generated decoder refuses what the schema and the Python codec refuse (T1.105): quoted or fractional integers,
 * integers outside the schema's bounds, a quoted boolean, an explicit null, a repeated key, text that is not JSON and
 * nesting over 35. Every case is a fixture of protocol/fixtures that the Python codec runs too; the expected text is
 * the reason, with the path of the field (EnvelopeFixtureRules.kt lists them; EnvelopeFixtureCoverageTest checks that
 * no fixture is missing from the lists).
 */
class EnvelopeStrictTest {
    private fun text(
        dir: String,
        name: String,
    ): String = fixtureFile(dir, name).readText()

    private fun refusal(text: String): SerializationException {
        val failing = Executable { decodeEnvelope(text) }
        return assertThrows(SerializationException::class.java, failing)
    }

    private fun assertRefused(
        text: String,
        reason: String,
        label: String,
    ) {
        val message = refusal(text).message.orEmpty()
        assertTrue(message.contains(reason, ignoreCase = true), "$label: '$reason' not in '$message'")
    }

    private fun frame(
        body: String,
        extra: String = "",
    ): String =
        """{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","type":"ack","ts":"2026-10-03T18:00:00Z","body":$body$extra}"""

    @Test
    fun `fixtures the schema or the codec refuse are refused with their reason`() {
        for ((dir, name, reason) in refusals) assertRefused(text(dir, name), reason, "$dir/$name")
    }

    @Test
    @OptIn(ExperimentalSerializationApi::class)
    fun `a missing body is a missing field`() {
        val missing = refusal(text("invalid", "missing-body"))
        assertInstanceOf(MissingFieldException::class.java, missing)
        assertTrue(missing.message.orEmpty().contains("body"), missing.message)
    }

    @Test
    fun `every raw-invalid frame is refused for its defect and is a good frame once the defect is repaired`() {
        val entries = indexEntries("raw-invalid")
        assertEquals(fixtureNames("raw-invalid").size, entries.size)
        assertTrue(entries.size >= 15, "raw-invalid entries found: ${entries.size}")
        for (entry in entries) {
            val name = entry.getValue("file").jsonPrimitive.content
            val raw = fixtureDir.resolve("raw-invalid").resolve(name).readText()
            val repaired =
                when (val defect = entry.getValue("defect").jsonPrimitive.content) {
                    "repeated-key" -> {
                        assertRefused(raw, "duplicate key", name)
                        Json.parseToJsonElement(raw).toString() // the parser merges the repeated key: last one wins
                    }

                    "not-json" -> {
                        assertRefused(raw, "not valid JSON", name)
                        repairedText(entry)
                    }

                    else -> {
                        error("$name: unknown defect '$defect'")
                    }
                }
            assertTrue(decodeEnvelope(repaired).body.frameType.isNotEmpty(), name)
        }
    }

    @Test
    fun `a repeated key is found with spaces around the colon, in any case of escaping`() {
        assertRefused(
            frame(
                """{"upToSeq" : 1 , "upToSeq"
            : 2}""",
            ),
            "duplicate key",
            "spaces",
        )
        assertRefused(frame("""{"upToSeq":1,"upTo\u0053eq":2}"""), "duplicate key", "escape")
        assertRefused(frame("""{"upToSeq":"\"","upToSeq":2}"""), "duplicate key", "escaped quote in a value")
        assertRefused(frame("""{"upToSeq":1}""", ""","v":1"""), "duplicate key", "envelope")
    }

    @Test
    fun `equal keys in different objects and brackets inside strings are not duplicates`() {
        val ok =
            """{"v":1,"id":"01JABCDEFGHJKMNPQRSTVWXYZ0","type":"skill.result","ts":"2026-10-03T18:00:00Z",""" +
                """"body":{"callId":"01JABCDEFGHJKMNPQRSTVWXYZ0","status":"ok","output":""" +
                """{"{":1,"[":2,"k\"":3,"a:b":{"{":4,"k\"":[{"x":1},{"x":2}]},"s":"]}\"{"}}}"""
        assertEquals("skill.result", decodeEnvelope(ok).body.frameType)
        assertEquals(AckBody(9007199254740991), decodeEnvelope(frame("""{"upToSeq":9007199254740991}""")).body)
        assertEquals(AckBody(0), decodeEnvelope(frame("""{"upToSeq":0}""")).body)
    }

    @Test
    fun `numbers that are not plain integer tokens are refused`() {
        val badNumbers =
            listOf(
                "true",
                "[1]",
                "{}",
                "1.5",
                "1E0",
                "1e+0",
                "-1",
                "99999999999999999999",
                "9007199254740992",
                "\"1\"",
            )
        for (number in badNumbers) assertRefused(frame("""{"upToSeq":$number}"""), "frame.body.upToSeq", number)
        for (spelling in listOf("+1", "01", "-01", "1.")) {
            assertRefused(frame("""{"upToSeq":$spelling}"""), "not valid JSON", spelling)
        }
        assertRefused(frame("""{"upToSeq":null}"""), "null is not allowed", "null")
    }

    @Test
    fun `text that is not a frame object is refused`() {
        for (garbage in listOf("[]", "null", "5", "\"ack\"", "", "{")) {
            assertThrows(SerializationException::class.java, Executable { decodeEnvelope(garbage) }, garbage)
        }
    }
}
