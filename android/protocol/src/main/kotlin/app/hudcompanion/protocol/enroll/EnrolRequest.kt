// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.util.Base64

/** Why [EnrolRequest.of] refused to build a body: for tests and a log code only. */
public enum class EnrolRequestRefusal {
    BAD_SPKI,
    BAD_CODE,
    BAD_SIGNATURE,
}

/**
 * The `POST /enroll` request body (`protocol/README.md` "HTTP `/enroll`"; T1.23c) and the key fingerprint shown while
 * it waits: both come from one copy of the SPKI bytes, so the fingerprint shown and the `spki` sent cannot diverge.
 */
public sealed interface EnrolRequest {
    /**
     * A body to send: one JSON object with exactly `spki`, `code`, `signature`, the binary values as canonical padded
     * standard base64. Not a data class: no `copy` can pair a [body] with another [fingerprint].
     */
    public class Ready internal constructor(
        public val body: String,
        public val fingerprint: String,
    ) : EnrolRequest {
        /** Leaves the body (and its one-time code) out of every log line. */
        override fun toString(): String = "Ready(fingerprint=$fingerprint)"
    }

    /** Nothing to send: only the [reason]. */
    public data class Refused(
        public val reason: EnrolRequestRefusal,
    ) : EnrolRequest

    public companion object {
        /** The server's body cap; the largest accepted inputs give about 520 bytes (the test proves the bound). */
        public const val MAX_BODY_BYTES: Int = 4096
        public const val MAX_SPKI_BYTES: Int = 256
        public const val MAX_SIGNATURE_BYTES: Int = 80

        /** Builds the body from the SPKI DER, the one-time [code] and the DER signature over [proofBytes]. */
        public fun of(
            spkiDer: ByteArray,
            code: String,
            signatureDer: ByteArray,
        ): EnrolRequest {
            // the spki copy is the concurrent-mutation guard of SAT-PL-34 (body and fingerprint come from one array
            // that another thread cannot change; EnrolRequestConcurrentMutationTest fails without it); the
            // signature copy keeps the size check and the encoding on one snapshot
            val spki = spkiDer.copyOf()
            val signature = signatureDer.copyOf()
            return when {
                spki.size !in 1..MAX_SPKI_BYTES -> Refused(EnrolRequestRefusal.BAD_SPKI)
                !enrolCodeShape.matches(code) -> Refused(EnrolRequestRefusal.BAD_CODE)
                signature.size !in 1..MAX_SIGNATURE_BYTES -> Refused(EnrolRequestRefusal.BAD_SIGNATURE)
                else -> Ready(body(spki, code, signature), keyFingerprint(spki))
            }
        }

        private fun body(
            spki: ByteArray,
            code: String,
            signature: ByteArray,
        ): String {
            val base64 = Base64.getEncoder()
            return buildJsonObject {
                put("spki", base64.encodeToString(spki))
                put("code", code)
                put("signature", base64.encodeToString(signature))
            }.toString()
        }
    }
}
