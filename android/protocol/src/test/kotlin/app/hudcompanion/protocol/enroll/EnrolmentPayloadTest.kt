// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.enroll.Payloads.CODE
import app.hudcompanion.protocol.enroll.Payloads.ORIGIN
import app.hudcompanion.protocol.enroll.Payloads.json
import app.hudcompanion.protocol.enroll.Payloads.link
import app.hudcompanion.protocol.enroll.Payloads.pin
import app.hudcompanion.protocol.enroll.Payloads.withUrl
import app.hudcompanion.protocol.json.JsonRefusal
import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertFalse
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import java.util.Base64

/**
 * SAT-PL-30 (T1.23a): every row is the one parser error with no partial result; the same valid
 * payload as app link, pasted link and pasted JSON gives equal results.
 */
class EnrolmentPayloadTest {
    private val parsed = EnrolmentPayload.Parsed(ORIGIN, "glasses.example.com", CODE, Payloads.pins)
    private val badFragment = EnrolmentPayload.Refused(EnrolmentRefusal.BAD_FRAGMENT)

    private fun refused(
        reason: EnrolmentRefusal,
        json: JsonRefusal? = null,
    ) = EnrolmentPayload.Refused(reason, json)

    private fun assertRows(rows: List<Pair<String, EnrolmentPayload>>) =
        assertAll(rows.map { (input, want) -> Executable { assertEquals(want, EnrolmentPayload.parse(input), input) } })

    @Test
    fun `the same payload as app link, pasted link and pasted JSON parses to equal results`() {
        assertRows(
            listOf(
                link(json()) to parsed,
                " \t${link(json())}\r\n" to parsed,
                json() to parsed,
                "\n  ${json()}  \n" to parsed,
                link("\n${json()} ") to parsed,
            ),
        )
    }

    @Test
    fun `input over 2048 UTF-8 bytes is refused, counted in bytes and not in UTF-16 units`() {
        val ascii = json() + " ".repeat(EnrolmentPayload.MAX_BYTES - json().length)
        val accented = withUrl("https://" + "é".repeat(1100) + ".example")
        assertRows(
            listOf(
                ascii to parsed,
                "$ascii " to refused(EnrolmentRefusal.TOO_LARGE),
                accented to refused(EnrolmentRefusal.TOO_LARGE),
                link(json() + " ".repeat(1500)) to refused(EnrolmentRefusal.TOO_LARGE),
            ),
        )
    }

    @Test
    fun `a link with another scheme, host, path, query, user info or port is refused`() {
        val fragment = link(json()).substringAfter('#')
        assertRows(
            listOf(
                "wayfinch:enroll#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://enrol#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://ENROLL#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://enroll/#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://enroll/x#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://enroll?a=1#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://u@enroll#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://enroll:1#$fragment" to refused(EnrolmentRefusal.BAD_LINK),
                "wayfinch://enroll" to refused(EnrolmentRefusal.BAD_LINK),
                "WAYFINCH://enroll#$fragment" to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.SYNTAX),
                "https://enroll#$fragment" to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.SYNTAX),
                "wayfinc://enroll#$fragment" to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.SYNTAX),
            ),
        )
    }

    @Test
    fun `a fragment with padding, a non-base64url character or bytes that are not UTF-8 is refused`() {
        val fragment = link(json()).substringAfter('#')
        assertTrue(Payloads.paddedLink().endsWith("="))
        assertRows(
            listOf(
                Payloads.paddedLink() to badFragment,
                "wayfinch://enroll#" to badFragment,
                "wayfinch://enroll#$fragment." to badFragment,
                "wayfinch://enroll#$fragment#" to badFragment,
                "wayfinch://enroll#$fragment " to parsed,
                "wayfinch://enroll#${fragment.dropLast(1)} ${fragment.last()}" to badFragment,
                "wayfinch://enroll#${Payloads.fragment(byteArrayOf(0xC3.toByte(), 0x28))}" to badFragment,
            ),
        )
    }

    @Test
    fun `a fragment that is not the canonical encoding of its bytes is refused`() {
        val text = if (json().length % 3 == 0) json() + " " else json()
        val fragment = link(text).substringAfter('#')
        val alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        val bumped = fragment.dropLast(1) + alphabet[alphabet.indexOf(fragment.last()) xor 1]
        val oneMod4 = fragment + "A".repeat((5 - fragment.length % 4) % 4)
        assertTrue(Base64.getUrlDecoder().decode(bumped).contentEquals(text.toByteArray()), "same bytes")
        assertEquals(1, oneMod4.length % 4)
        assertRows(
            listOf(
                link(text) to parsed,
                "wayfinch://enroll#$bumped" to badFragment,
                "wayfinch://enroll#$oneMod4" to badFragment,
            ),
        )
    }

    @Test
    fun `only ASCII whitespace is trimmed, no other Unicode space`() {
        assertRows(
            listOf(
                "\u00A0" + json() to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.SYNTAX),
                json() + "\u3000" to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.TRAILING),
                link(json()) + "\u2028" to badFragment,
                "\u000B" + link(json()) to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.SYNTAX),
            ),
        )
    }

    @Test
    fun `a standard base64 character in the fragment is refused`() {
        val fragment = link(json()).substringAfter('#')
        assertRows(listOf("wayfinch://enroll#+$fragment", "wayfinch://enroll#/$fragment").map { it to badFragment })
    }

    @Test
    fun `an extra, missing or repeated key is refused, also a key repeated through a u escape`() {
        val body = json().removeSuffix("}")
        val duplicate = refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.DUPLICATE_KEY)
        assertRows(
            listOf(
                "$body,\"extra\":1}" to refused(EnrolmentRefusal.BAD_KEYS),
                json().replace("\"version\":1,", "") to refused(EnrolmentRefusal.BAD_KEYS),
                "$body,\"code\":\"$CODE\"}" to duplicate,
                "$body,\"c\\u006fde\":\"$CODE\"}" to duplicate,
                link("$body,\"\\u0063ode\":\"$CODE\"}") to duplicate,
                "{}" to refused(EnrolmentRefusal.BAD_KEYS),
            ),
        )
    }

    @Test
    fun `a version other than the integer 1 is refused`() {
        val rows = listOf("1.0", "1e0", "10E-1", "\"1\"", "2", "0", "-1", "true", "null", "[1]")
        assertRows(rows.map { json(version = it) to refused(EnrolmentRefusal.BAD_VERSION) })
    }

    @Test
    fun `a url that is not an https origin is refused`() {
        val rows =
            listOf(
                "http://glasses.example.com",
                "HTTPS://glasses.example.com",
                "wss://glasses.example.com",
                "https:glasses.example.com",
                "https://u@glasses.example.com",
                "https://u:p@glasses.example.com",
                "https://glasses.example.com/enroll",
                "https://glasses.example.com//",
                "https://glasses.example.com?q",
                "https://glasses.example.com/?q",
                "https://glasses.example.com#f",
                "https://glasses.example.com/#f",
                "https://glasses.example.com:0",
                "https://glasses.example.com:65536",
                "https://glasses.example.com:08443",
                "https://glasses.example.com:",
                "https://glasses.example.com:x",
            ).map { withUrl(it) to refused(EnrolmentRefusal.BAD_URL) }
        val noHost =
            listOf("https://", "https:///", "https://:443").map { withUrl(it) to refused(EnrolmentRefusal.BAD_HOST) }
        assertRows(rows + noHost + (json(url = "1") to refused(EnrolmentRefusal.BAD_URL)))
    }

    @Test
    fun `a code that is not 22 base64url characters is refused`() {
        val short = CODE.dropLast(1)
        val surrogates = listOf("$short\\uD800", "$short\\uDC00")
        val rows = listOf(CODE.drop(1), CODE + "A", "$short+", "$short=", "${short}é") + surrogates
        val texts = rows.map { "\"$it\"" } + listOf("1", "null", "\"\"")
        assertRows(texts.map { json(code = it) to refused(EnrolmentRefusal.BAD_CODE) })
    }

    @Test
    fun `zero or nine pins, a pin without sha256 or not 32 bytes, a repeated or non-canonical pin are refused`() {
        val raw = Base64.getEncoder().encodeToString(ByteArray(32))
        val nine = (1..9).map { "\"${pin(it)}\"" }.joinToString(",", "[", "]")
        val eight = (1..8).map { "\"${pin(it)}\"" }.joinToString(",", "[", "]")
        val rows =
            listOf(
                "[]",
                nine,
                "[\"$raw\"]",
                "[\"sha1/$raw\"]",
                "[\"SHA256/$raw\"]",
                "[\"sha256/${Base64.getEncoder().encodeToString(ByteArray(31))}\"]",
                "[\"sha256/${Base64.getEncoder().encodeToString(ByteArray(33))}\"]",
                "[\"sha256/${raw.removeSuffix("=")}\"]",
                "[\"sha256/${raw.dropLast(2)}B=\"]",
                "[\"sha256/${Base64.getUrlEncoder().encodeToString(ByteArray(32) { -1 })}\"]",
                "[\"${pin(1)}\",\"${pin(1)}\"]",
                "[1]",
                "\"${pin(1)}\"",
            )
        assertRows(rows.map { json(pins = it) to refused(EnrolmentRefusal.BAD_PINS) })
        assertEquals(8, (EnrolmentPayload.parse(json(pins = eight)) as EnrolmentPayload.Parsed).pins.size)
    }

    @Test
    fun `NaN, trailing bytes after the JSON and a BOM are refused`() {
        assertRows(
            listOf(
                json(version = "NaN") to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.SYNTAX),
                json() + "x" to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.TRAILING),
                json() + "{}" to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.TRAILING),
                link(json() + "x") to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.TRAILING),
                "\uFEFF" + json() to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.BOM),
                link("\uFEFF" + json()) to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.BOM),
                "\uFEFF" + link(json()) to refused(EnrolmentRefusal.BAD_JSON, JsonRefusal.BOM),
            ),
        )
    }

    @Test
    fun `neither result prints the one-time code or input text`() {
        assertFalse(CODE in EnrolmentPayload.parse(json()).toString())
        assertFalse(CODE in EnrolmentPayload.parse(json() + "x").toString())
    }
}
