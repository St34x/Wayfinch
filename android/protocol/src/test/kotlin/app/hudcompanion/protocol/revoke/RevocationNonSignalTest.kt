// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.revoke

import org.junit.jupiter.api.Assertions.assertEquals
import org.junit.jupiter.api.Test
import java.io.ByteArrayInputStream

/**
 * SAT-PL-73, classification half (T1.24d): the non-signal table is close `1000`, `1001`,
 * `1008`, `1009`, `1011`, `4400`, `4401`, `4409`, `4410`; HTTP `401`, `429`, `503`; the unmarked `403` variants of
 * SAT-PL-70. Each is classified `NotRevoked` (or `AccessDenied` for a `403`), never `Revoked`. Close `1013` (the
 * server could not check the device) is in the table too (`protocol/README.md`); `4402`, `4404` and `403` as a close
 * code are extra probes.
 */
class RevocationNonSignalTest {
    private fun upgrade(
        status: Int,
        contentType: String,
        body: String,
    ): RevocationSignal =
        RevocationMarker.classifyUpgrade(
            status,
            contentType,
            ByteArrayInputStream(body.toByteArray(Charsets.UTF_8)),
        )

    @Test
    fun `no close code but 4403 is a signal`() {
        for (code in NON_SIGNAL_CLOSES) {
            assertEquals(RevocationSignal.NotRevoked, RevocationMarker.classifyClose(code), "close $code")
        }
    }

    @Test
    fun `the server's 401, 429 and 503 answers are not a signal`() {
        assertEquals(RevocationSignal.NotRevoked, upgrade(401, JSON, "{\"error\":\"unauthorized\"}"))
        assertEquals(RevocationSignal.NotRevoked, upgrade(429, "text/html", "<html>429</html>"))
        assertEquals(RevocationSignal.NotRevoked, upgrade(503, JSON, "{\"error\":\"unavailable\"}"))
    }

    @Test
    fun `unmarked 403 variants are access denied, never revoked`() {
        val variants =
            listOf(
                JSON to "",
                "text/html" to "<html>403</html>",
                JSON to "{\"error\":\"forbidden\"}",
                "text/plain" to "{\"error\":\"device_revoked\"}",
                JSON to "{\"error\":\"device_revoked\"" + " ".repeat(1100) + "}",
            )
        for ((type, body) in variants) {
            assertEquals(RevocationSignal.AccessDenied, upgrade(403, type, body), "$type ${body.length}")
        }
    }

    private companion object {
        const val JSON = "application/json"
        val NON_SIGNAL_CLOSES = listOf(1000, 1001, 1008, 1009, 1011, 1013, 4400, 4401, 4409, 4410, 4402, 4404, 403)
    }
}
