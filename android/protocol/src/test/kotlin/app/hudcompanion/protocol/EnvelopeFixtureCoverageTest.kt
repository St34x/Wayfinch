// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol

import app.hudcompanion.protocol.generated.decodeEnvelope
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.jsonPrimitive
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test

/**
 * Every refused frame of protocol/fixtures (`invalid/`, `codec-invalid/`, `raw-invalid/`) is either refused by
 * `decodeEnvelope` with a reason asserted in EnvelopeStrictTest, or named in `knownGaps` (EnvelopeFixtureRules.kt) with
 * the rule it breaks, which protocol/README.md lists as a gap of the Kotlin decoder. A fixture that leaves the gap
 * list (the decoder now refuses it) or a new fixture the decoder accepts fails this test.
 */
class EnvelopeFixtureCoverageTest {
    private val dirs = listOf("invalid", "codec-invalid", "raw-invalid")

    private fun refused(key: String): Boolean {
        val text = fixtureDir.resolve("$key.json").readText()
        return runCatching { decodeEnvelope(text) }.exceptionOrNull() is SerializationException
    }

    private val all = dirs.flatMap { dir -> fixtureNames(dir).map { "$dir/$it" } }

    @Test
    fun `the fixtures the decoder accepts are exactly the known gaps`() {
        val gaps = knownGaps.values.flatten()
        assertEquals(gaps.toSet().size, gaps.size, "a fixture is named under two rules")
        assertTrue(all.size >= 70, "fixtures found: ${all.size}")
        assertEquals(gaps.sorted(), all.filterNot(::refused).sorted(), "accepted fixtures vs knownGaps")
    }

    @Test
    fun `the fixtures the decoder refuses are exactly the ones whose reason a test asserts`() {
        val raw =
            indexEntries("raw-invalid").map {
                it
                    .getValue("file")
                    .jsonPrimitive.content
                    .removeSuffix(".json")
            }
        val asserted =
            refusals.map { "${it.first}/${it.second}" } + refusedWithoutReason +
                raw.map { "raw-invalid/$it" }
        assertEquals(asserted.toSet().size, asserted.size, "a fixture is asserted twice")
        assertEquals(asserted.sorted(), all.filter(::refused).sorted(), "refused fixtures vs asserted reasons")
    }

    @Test
    fun `every rule of the gap list is named in the README of the protocol`() {
        val readme =
            fixtureDir.parentFile
                .resolve("README.md")
                .readText()
                .split(Regex("\\s+"))
                .joinToString(" ")
        for (rule in knownGaps.keys) assertTrue(readme.contains(rule), "protocol/README.md lacks the gap '$rule'")
    }
}
