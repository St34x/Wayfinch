// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.enroll.Payloads.CODE
import app.hudcompanion.protocol.enroll.Payloads.json
import app.hudcompanion.protocol.enroll.Payloads.link
import app.hudcompanion.protocol.enroll.Payloads.withUrl
import app.hudcompanion.protocol.json.JsonRefusal
import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import kotlin.random.Random

/** Boundary, truncated, mutated and oversized inputs of [EnrolmentPayload.parse]: one result, never an exception. */
class EnrolmentInputEdgeCasesTest {
    private val label63 = "a".repeat(LABEL_MAX)

    private fun parse(input: String): EnrolmentPayload = EnrolmentPayload.parse(input)

    private fun refusedUrls(
        reason: EnrolmentRefusal,
        urls: List<String>,
    ) = assertAll(
        urls.map { url ->
            Executable { assertEquals(EnrolmentPayload.Refused(reason), parse(withUrl(url)), url) }
        },
    )

    private fun parsedHost(url: String): String? = (parse(withUrl(url)) as? EnrolmentPayload.Parsed)?.host

    /** A link of exactly [length] characters whose JSON is padded with trailing spaces. */
    private fun linkOfLength(length: Int): String {
        val fragmentLength = length - "wayfinch://enroll#".length
        val bytes = fragmentLength / 4 * 3 + (fragmentLength % 4).let { if (it == 0) 0 else it - 1 }
        return link(json().padEnd(bytes, ' ')).also { assertEquals(length, it.length) }
    }

    @Test
    fun `a link of exactly 2048 characters is read and one of 2049 is too large`() {
        assertTrue(parse(linkOfLength(EnrolmentPayload.MAX_BYTES)) is EnrolmentPayload.Parsed)
        assertEquals(
            EnrolmentPayload.Refused(EnrolmentRefusal.TOO_LARGE),
            parse(linkOfLength(EnrolmentPayload.MAX_BYTES + 1)),
        )
    }

    @Test
    fun `a lone surrogate counts three bytes and a four byte character four against the bound`() {
        val room = EnrolmentPayload.MAX_BYTES - json().length
        val tooLarge = EnrolmentPayload.Refused(EnrolmentRefusal.TOO_LARGE)
        val trailing = EnrolmentPayload.Refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.TRAILING)
        assertEquals(trailing, parse(json() + " ".repeat(room - 3) + "\uD800"))
        assertEquals(tooLarge, parse(json() + " ".repeat(room - 2) + "\uD800"))
        assertEquals(trailing, parse(json() + " ".repeat(room - 4) + "\uD83D\uDE00"))
        assertEquals(tooLarge, parse(json() + " ".repeat(room - 3) + "\uD83D\uDE00"))
    }

    @Test
    fun `megabytes of input are refused as too large without being read`() {
        val tooLarge = EnrolmentPayload.Refused(EnrolmentRefusal.TOO_LARGE)
        listOf("A".repeat(HUGE), "wayfinch://enroll#" + "A".repeat(HUGE), "{".repeat(HUGE), " ".repeat(HUGE) + json())
            .forEach { assertEquals(tooLarge, parse(it)) }
    }

    @Test
    fun `empty and whitespace only input and bare link prefixes are refused`() {
        val inputs =
            listOf("", " ", "\r\n\t", "wayfinch:", "wayfinch:/", "wayfinch://", "wayfinch://enroll", "{", "[]", "null")
        inputs.forEach { assertTrue(parse(it) is EnrolmentPayload.Refused, it) }
    }

    @Test
    fun `hosts at the length limits`() {
        val host253 = listOf(label63, label63, label63, "a".repeat(LABEL_MAX - 2)).joinToString(".")
        val host254 = listOf(label63, label63, label63, "a".repeat(LABEL_MAX - 1)).joinToString(".")
        assertEquals(253, host253.length)
        assertEquals(host253, parsedHost("https://$host253"))
        refusedUrls(
            EnrolmentRefusal.BAD_HOST,
            listOf("https://$host254", "https://${label63}a.example"),
        )
    }

    @Test
    fun `hosts that are not plain DNS labels are refused`() {
        refusedUrls(
            EnrolmentRefusal.BAD_HOST,
            listOf(
                "https://a..b",
                "https://.a",
                "https://a.",
                "https://-a.example",
                "https://a-.example",
                "https://a_b.example",
                "https://a b.example",
                "https://a\\tb.example",
                "https://a\\\\b.example",
                "https://a%41.example",
                "https://\uFF41.example",
                "https://glasses.example.com\\u0000",
                "https://glasses.example.com\\n",
                "https://glasses.example.com ",
            ),
        )
        listOf("https://[::1]", "https://[::1]:443", "https://[2001:db8::1]/").forEach {
            assertTrue(parse(withUrl(it)) is EnrolmentPayload.Refused, it)
        }
    }

    @Test
    fun `ports at the limits and ports that are not decimal numbers`() {
        assertEquals("a.example", parsedHost("https://a.example:1"))
        assertEquals("a.example", parsedHost("https://a.example:65535/"))
        val badPorts = listOf(":80:80", ":-1", ":+80", ":1_0", ":1 ", ":\u0668\u0660", ":999999", ":0443", ":00")
        refusedUrls(EnrolmentRefusal.BAD_URL, badPorts.map { "https://a.example$it" })
    }

    @Test
    fun `the origin keeps the port and drops the slash, the host shows neither, a host is shown as written`() {
        val parsed = parse(withUrl("https://Glasses.Example.com:8443/")) as EnrolmentPayload.Parsed
        assertEquals("https://Glasses.Example.com:8443", parsed.origin)
        assertEquals("Glasses.Example.com", parsed.host)
        assertEquals("xn--e1afmkfd.example", parsedHost("https://xn--e1afmkfd.example"))
        assertEquals("127.0.0.1", parsedHost("https://127.0.0.1:8443/"))
    }

    @Test
    fun `values of the wrong JSON type are refused with the reason of their key`() {
        val wrong = listOf("1", "null", "true", "[]", "{}", "[\"https://a.example\"]")
        assertAll(
            wrong.map { Executable { assertEquals(refused(EnrolmentRefusal.BAD_URL), parse(json(url = it)), it) } },
        )
        listOf("null", "{}", "\"x\"", "true", "1").forEach {
            assertEquals(refused(EnrolmentRefusal.BAD_PINS), parse(json(pins = it)), it)
        }
        listOf("[null]", "[true]", "[1,2]").forEach {
            assertEquals(refused(EnrolmentRefusal.BAD_PINS), parse(json(pins = it)), it)
        }
        listOf("[[]]", "[{}]", "[[[]]]").forEach {
            assertEquals(refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.TOO_DEEP), parse(json(pins = it)), it)
        }
    }

    @Test
    fun `keys are exact, in any order, and nesting beyond the payload shape is refused`() {
        val pins = Payloads.pins.joinToString(",", "[", "]") { "\"$it\"" }
        val reversed = "{\"pins\":$pins,\"code\":\"$CODE\",\"url\":\"${Payloads.ORIGIN}\",\"version\":1}"
        assertTrue(parse(reversed) is EnrolmentPayload.Parsed)
        listOf("Version", "VERSION", "version ", " url", "Pins").forEach {
            val renamed = json().replaceFirst("\"${it.trim().lowercase()}\"", "\"$it\"")
            assertEquals(refused(EnrolmentRefusal.BAD_KEYS), parse(renamed), it)
        }
        val deep = json(pins = "[".repeat(DEEP_PINS))
        assertEquals(refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.TOO_DEEP), parse(deep))
    }

    @Test
    fun `every strict prefix of a valid link or JSON is refused`() {
        listOf(link(json()), json()).forEach { valid ->
            assertTrue(parse(valid) is EnrolmentPayload.Parsed)
            (0 until valid.length).forEach { cut ->
                assertTrue(parse(valid.substring(0, cut)) is EnrolmentPayload.Refused, "$cut of $valid")
            }
        }
    }

    @Test
    fun `a payload with one character replaced never throws and is parsed only into well formed fields`() {
        val random = Random(SEED)
        val noise = "\"\\{}[]:,.-_=/+ %@#?\u00e9\u0000\uD800aZ09"
        listOf(link(json()), json()).forEach { valid ->
            valid.indices.forEach { index ->
                repeat(NOISE_PER_POSITION) {
                    val mutated =
                        valid.substring(0, index) + noise[random.nextInt(noise.length)] + valid.substring(index + 1)
                    assertWellFormed(parse(mutated), mutated)
                }
            }
        }
    }

    private fun assertWellFormed(
        result: EnrolmentPayload,
        input: String,
    ) {
        if (result !is EnrolmentPayload.Parsed) return
        assertTrue(Regex("[A-Za-z0-9_-]{22}").matches(result.code), input)
        assertTrue(result.pins.size in 1..MAX_PINS && result.pins.toSet().size == result.pins.size, input)
        assertTrue(
            result.origin.startsWith("https://${result.host}") && result.host.all { it.code in 0x21..0x7E },
            input,
        )
    }

    private fun refused(
        reason: EnrolmentRefusal,
        json: JsonRefusal? = null,
    ) = EnrolmentPayload.Refused(reason, json)

    private companion object {
        const val LABEL_MAX = 63
        const val HUGE = 5_000_000
        const val DEEP_PINS = 1_000
        const val SEED = 20261023
        const val NOISE_PER_POSITION = 3
        const val MAX_PINS = 8
    }
}
