// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.enroll.Payloads.withUrl
import org.junit.jupiter.api.Assertions.assertAll
import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Assertions.assertTrue
import org.junit.jupiter.api.Test
import org.junit.jupiter.api.function.Executable
import kotlin.random.Random

/**
 * Edge cases for the `url` of an enrolment payload (SAT-PL-31): host forms that a resolver
 * may read as an IPv4 address, IPv6 brackets, trailing dots, upper-case hex, port bounds, user info and scheme
 * spellings. A refusal is checked with its reason where the reason is part of the contract, else as "any refusal".
 */
class EnrolmentUrlAdversarialTest {
    private fun parse(url: String): EnrolmentPayload = EnrolmentPayload.parse(withUrl(url))

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

    private fun assertShown(
        url: String,
        origin: String,
        host: String,
    ) {
        val result = parse(url) as? EnrolmentPayload.Parsed
        assertEquals(origin to host, result?.let { it.origin to it.host }, url)
    }

    @Test
    fun `a host keeps its case and a port stays in the origin but not in the host shown`() {
        assertShown("https://EXAMPLE.COM", "https://EXAMPLE.COM", "EXAMPLE.COM")
        assertShown("https://example.com:443/", "https://example.com:443", "example.com")
        assertShown("https://example.com:1", "https://example.com:1", "example.com")
        assertShown("https://example.com:65535", "https://example.com:65535", "example.com")
        assertShown("https://192.0.2.7:65535/", "https://192.0.2.7:65535", "192.0.2.7")
    }

    @Test
    fun `a port outside 1 to 65535 or not plain ASCII decimal digits is a bad url`() {
        val ports =
            listOf(
                "0",
                "65536",
                "99999",
                "100000",
                "00443",
                "+443",
                "-1",
                " 443",
                "443 ",
                "4_43",
                "0x1BB",
                "\u0664\u0664\u0663",
                "\uFF14\uFF14\uFF13",
                "443:443",
                ":443",
                "4.43",
                "443.",
                "4294967296",
            )
        assertRefused(ports.map { "https://a.example:$it" }, EnrolmentRefusal.BAD_URL)
        assertRefused(ports.map { "https://192.0.2.7:$it/" }, EnrolmentRefusal.BAD_URL)
    }

    @Test
    fun `a host that ends in a decimal number or in 0x is refused unless it is a canonical dotted quad`() {
        val hosts =
            listOf(
                "0",
                "00",
                "1",
                "256",
                "4294967296",
                "0.0.0",
                "1.2.3",
                "1.2.3.4.5",
                "256.0.0.1",
                "1.256.0.1",
                "1.2.3.04",
                "01.2.3.4",
                "000.0.0.0",
                "1.2.3.1000",
                "0x0",
                "0XFF",
                "0xFF.0xFF.0xFF.0xFF",
                "0xdeadbeef",
                "0XDEADBEEF",
                "0x7f.0.0.1",
                "192.168.0x1",
                "192.168.1.0x1",
                "example.0",
                "example.com.0",
                "example.0X7F",
                "example.0x",
                "0x1g",
            )
        assertRefused(hosts.map { "https://$it" }, EnrolmentRefusal.BAD_HOST)
        assertRefused(hosts.map { "https://$it:443/" }, EnrolmentRefusal.BAD_HOST)
    }

    @Test
    fun `a number or hex label that is not the last label leaves the host a DNS name`() {
        assertShown("https://0X7F.1.example.com", "https://0X7F.1.example.com", "0X7F.1.example.com")
        assertShown("https://1.2.3.4.example.com", "https://1.2.3.4.example.com", "1.2.3.4.example.com")
        assertShown("https://0x.example.com", "https://0x.example.com", "0x.example.com")
        assertShown("https://a1.2b", "https://a1.2b", "a1.2b")
        assertShown("https://example.1e3", "https://example.1e3", "example.1e3")
    }

    @Test
    fun `a trailing dot or an empty label is refused on a name and on a dotted quad`() {
        val hosts =
            listOf(
                "example.com.",
                "192.0.2.7.",
                "192.0.2.",
                ".192.0.2.7",
                "1..2.3",
                ".",
                "..",
                "example.com..",
                ".example.com",
            )
        assertRefused(hosts.map { "https://$it" }, EnrolmentRefusal.BAD_HOST)
        assertRefused(hosts.map { "https://$it:443" }, EnrolmentRefusal.BAD_HOST)
    }

    @Test
    fun `an IPv6 literal in any spelling is refused`() {
        val hosts =
            listOf(
                "[::1]",
                "[::1]:8443",
                "[::]",
                "[2001:db8::1]:443/",
                "[::ffff:127.0.0.1]",
                "[::FFFF:7F00:1]",
                "[fe80::1%25eth0]",
                "[1.2.3.4]",
                "[]",
                "[::1",
                "::1]",
                "::1",
                "::1:443",
            )
        assertRefused(hosts.map { "https://$it" }, null)
    }

    @Test
    fun `user info in any position is a bad url, never a host`() {
        val urls =
            listOf(
                "https://u@h.example",
                "https://@h.example",
                "https://h.example@",
                "https://u:p@h.example:443",
                "https://h.example:443@evil.example",
                "https://good.example@evil.example",
                "https://h.example\\\\@evil.example",
                "https://h.example/@x",
                "https://h.example?@x",
                "https://h.example#@x",
            )
        assertRefused(urls, EnrolmentRefusal.BAD_URL)
    }

    @Test
    fun `a backslash or a percent escape in the authority is a bad host`() {
        val urls =
            listOf(
                "https://good.example\\\\evil.example",
                "https://good.example%40evil.example",
                "https://good.example%2eevil.example",
                "https://good.example%5cevil.example",
                "https://%67ood.example",
            )
        assertRefused(urls, EnrolmentRefusal.BAD_HOST)
    }

    @Test
    fun `the scheme is lower case https with two slashes and nothing in front`() {
        val urls =
            listOf(
                "HTTPS://h.example",
                "Https://h.example",
                "http://h.example",
                "https:/h.example",
                "https:h.example",
                "https:///h.example",
                "//h.example",
                "h.example",
                "wss://h.example",
                " https://h.example",
                "https ://h.example",
            )
        assertRefused(urls, EnrolmentRefusal.BAD_URL)
    }

    @Test
    fun `an origin accepts the empty path and one slash only`() {
        assertShown("https://h.example/", "https://h.example", "h.example")
        val paths = listOf("//", "/.", "/%2e", "?", "#", "/?", "/#", "/x")
        assertRefused(paths.map { "https://h.example$it" }, EnrolmentRefusal.BAD_URL)
    }

    /** Random authorities: what the parser accepts is plain LDH, and a number-ending host is a canonical quad. */
    @Test
    fun `random authorities never yield a host that a resolver could read as another address`() {
        val random = Random(SEED)
        val authorities = List(ROUNDS) { randomAuthority(random) }
        val accepted = authorities.filter { parsedOrNull(it) != null }
        assertTrue(accepted.size > MIN_ACCEPTED, "the generator produced only ${accepted.size} valid hosts")
        assertAll(
            accepted.map { authority ->
                Executable {
                    val parsed = parsedOrNull(authority)
                    assertTrue(parsed != null && hostIsSafe(parsed), authority)
                    assertTrue(parsed != null && "https://$authority".startsWith(parsed.origin), authority)
                }
            },
        )
    }

    private fun parsedOrNull(authority: String): EnrolmentPayload.Parsed? =
        parse("https://$authority") as? EnrolmentPayload.Parsed

    private fun randomAuthority(random: Random): String {
        val alphabet = "abcdefxX0123456789....::@[]/-_%"
        return CharArray(random.nextInt(1, MAX_LENGTH)) { alphabet[random.nextInt(alphabet.length)] }.concatToString()
    }

    private fun hostIsSafe(parsed: EnrolmentPayload.Parsed): Boolean {
        val host = parsed.host
        val last = host.substringAfterLast('.')
        val numeric = last.all { it in '0'..'9' } || last.startsWith("0x") || last.startsWith("0X")
        val quad = Regex("(0|[1-9][0-9]{0,2})(\\.(0|[1-9][0-9]{0,2})){3}")
        val plain = host.all { it in 'A'..'Z' || it in 'a'..'z' || it in '0'..'9' || it == '-' || it == '.' }
        return plain && (!numeric || (quad.matches(host) && host.split('.').all { it.toInt() <= OCTET_MAX }))
    }

    private companion object {
        const val SEED = 20261023L
        const val ROUNDS = 20000
        const val MIN_ACCEPTED = 200
        const val MAX_LENGTH = 14
        const val OCTET_MAX = 255
    }
}
