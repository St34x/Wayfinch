// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

/** The `url` of an enrolment payload split into what the app uses: the origin it posts to and the host it shows. */
internal class HttpsOrigin(
    val origin: String,
    val host: String,
) {
    internal companion object {
        private const val SCHEME = "https://"
        private const val MAX_HOST = 253
        private const val MAX_LABEL = 63
        private const val MAX_PORT = 65535
        private const val MAX_OCTET = 255
        private const val QUAD = 4
        private val port = Regex("[1-9][0-9]{0,4}")
        private val label = Regex("[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?")
        private val numberLabel = Regex("[0-9]+|0[xX].*")
        private val octet = Regex("0|[1-9][0-9]{0,2}")

        /**
         * Reads [url] as `https://<host>[:<port>]` with an empty path or `/`: no user info, query or fragment, the
         * scheme in lower case, the port 1-65535 without leading zeros. The host is dot-separated labels of ASCII
         * letters, digits and hyphens (so an IDN only as its `xn--` A-label). A host whose last label is all decimal
         * digits or starts with `0x`/`0X` is one a resolver may read as IPv4, so it passes only as a dotted quad of
         * four decimal octets 0-255 without leading zeros, and the host shown is the address the phone connects to
         * (`2130706433`, `0x7f.1`, `127.1`, `0177.0.0.1` and `1.2.3.256` are refused). An IPv6 literal, any other
         * character (control, whitespace, non-ASCII, `%`) and an empty label are refused.
         */
        fun parse(url: String): HttpsOrigin {
            if (!url.startsWith(SCHEME)) refusePayload(EnrolmentRefusal.BAD_URL)
            val rest = url.substring(SCHEME.length)
            val end = rest.indexOfFirst { it in "/?#" }.let { if (it < 0) rest.length else it }
            val authority = rest.substring(0, end)
            if (rest.substring(end) !in setOf("", "/") || '@' in authority) refusePayload(EnrolmentRefusal.BAD_URL)
            val host = authority.substringBefore(':')
            if (host != authority) checkPort(authority.substringAfter(':'))
            if (!validHost(host)) refusePayload(EnrolmentRefusal.BAD_HOST)
            return HttpsOrigin(SCHEME + authority, host)
        }

        private fun checkPort(text: String) {
            if (!port.matches(text) || text.toInt() > MAX_PORT) refusePayload(EnrolmentRefusal.BAD_URL)
        }

        private fun validHost(host: String): Boolean {
            val labels = host.split('.')
            val ldh = host.length in 1..MAX_HOST && labels.all { it.length <= MAX_LABEL && label.matches(it) }
            return ldh && (!numberLabel.matches(labels.last()) || isDottedQuad(labels))
        }

        /** Four canonical decimal octets: `192.0.2.7`, never `0177.0.0.1`, `127.1` or `1.2.3.256`. */
        private fun isDottedQuad(labels: List<String>): Boolean =
            labels.size == QUAD && labels.all { octet.matches(it) && it.toInt() <= MAX_OCTET }
    }
}
