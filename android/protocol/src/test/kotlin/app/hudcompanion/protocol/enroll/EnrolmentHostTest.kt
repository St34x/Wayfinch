// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.enroll.Payloads.withUrl
import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable

/**
 * SAT-PL-31 (T1.23a): the host the app shows is the `url` host; an IDN
 * passes only as its A-label, so `xn--` stays visible; non-ASCII, control and whitespace characters are refused; a
 * host that ends in a number passes only as a canonical dotted quad.
 */
class EnrolmentHostTest {
    private fun parse(url: String) = EnrolmentPayload.parse(withUrl(url))

    private fun assertShown(
        url: String,
        origin: String,
        host: String,
    ) {
        val result = parse(url) as? EnrolmentPayload.Parsed
        assertEquals(origin to host, result?.let { it.origin to it.host }, url)
    }

    @Test
    fun `the host shown is the url host, an A-label stays visible`() {
        assertShown("https://glasses.example.com", "https://glasses.example.com", "glasses.example.com")
        assertShown("https://glasses.example.com:8443/", "https://glasses.example.com:8443", "glasses.example.com")
        assertShown("https://xn--bcher-kva.example/", "https://xn--bcher-kva.example", "xn--bcher-kva.example")
        assertShown("https://192.0.2.7:443", "https://192.0.2.7:443", "192.0.2.7")
        assertShown("https://Glasses.Example.COM:65535", "https://Glasses.Example.COM:65535", "Glasses.Example.COM")
        assertShown("https://127.0.0.1:8443", "https://127.0.0.1:8443", "127.0.0.1")
        assertShown("https://0.0.0.0", "https://0.0.0.0", "0.0.0.0")
        assertShown("https://255.255.255.255", "https://255.255.255.255", "255.255.255.255")
        // a number or hex-looking label elsewhere than last keeps the host a DNS name
        assertShown("https://0x7f.1.example.com", "https://0x7f.1.example.com", "0x7f.1.example.com")
    }

    /** A resolver reads these as IPv4 (127.0.0.1, 10.0.0.1, ...): shown verbatim, they would hide the real address. */
    @Test
    fun `SAT-PL-31 a host ending in a number passes only as a canonical dotted quad`() {
        val hosts =
            listOf(
                "2130706433",
                "0x7f.1",
                "127.1",
                "10.1",
                "0177.0.0.1",
                "1.2.3.256",
                "0X7F.0.0.1",
                "0x7f000001",
                "0x",
                "127.0.0.01",
                "1.2.3.4.5",
                "999.1.1.1",
                "example.0x1",
                "example.123",
                // fullwidth and Arabic-Indic digits, ideographic and fullwidth dots: not ASCII, so not a canonical quad
                "\uFF11\uFF12\uFF17.0.0.1",
                "127\u30020.0.0.1",
                "127\uFF0E0.0.0.1",
                "127\uFF610.0.0.1",
                "\u0664\u0664.0.0.1",
            )
        assertRefused(hosts.map { "https://$it" }, EnrolmentRefusal.BAD_HOST)
        assertRefused(listOf("https://2130706433:443/", "https://127.1:8443"), EnrolmentRefusal.BAD_HOST)
    }

    @Test
    fun `a non-ASCII host is refused`() {
        val hosts = listOf("bücher.example", "аpple.com", "glasses.example.cоm", "例え.jp", "glasses\u00ADexample.com")
        assertRefused(hosts.map { "https://$it" }, EnrolmentRefusal.BAD_HOST)
    }

    @Test
    fun `control or whitespace characters in the host are refused`() {
        // JSON escapes: the url string holds the real character after the reader unescapes it
        val escaped = listOf("\\u0000", "\\u001f", "\\u007f", "\\t", "\\n", "\\r", "\\u00a0", "\\u200b", "\\u2028")
        val inserts = escaped + listOf(" ", "\u00A0", "\u3000", "\u007F")
        assertRefused(inserts.map { "https://glasses$it.example.com" }, EnrolmentRefusal.BAD_HOST)
        assertRefused(listOf("https://glasses.example.com ", " https://glasses.example.com"), null)
    }

    @Test
    fun `a host that is not dot-separated LDH labels is refused`() {
        val hosts =
            listOf(
                "glasses..example.com",
                ".glasses.example.com",
                "glasses.example.com.",
                "-glasses.example.com",
                "glasses-.example.com",
                "glasses_x.example.com",
                "glasses%2eexample.com",
                "a".repeat(64) + ".example.com",
                ("a".repeat(63) + ".").repeat(4) + "com",
            )
        assertRefused(hosts.map { "https://$it" }, EnrolmentRefusal.BAD_HOST)
        assertRefused(listOf("https://[::1]", "https://[2001:db8::1]:443/"), null)
    }

    @Test
    fun `an origin with a path, query, fragment or user info is refused`() {
        assertRefused(listOf("https://h/x", "https://h/?q", "https://h/#f", "https://u@h"), EnrolmentRefusal.BAD_URL)
    }

    private fun assertRefused(
        urls: List<String>,
        reason: EnrolmentRefusal?,
    ) = assertAll(
        urls.map { url ->
            Executable {
                val result = parse(url)
                val refused = result as? EnrolmentPayload.Refused
                assertEquals(true, refused != null && (reason == null || refused.reason == reason), "$url -> $result")
            }
        },
    )
}
