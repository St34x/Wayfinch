// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.json.StrictJson
import app.hudcompanion.protocol.json.StrictJsonResult
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.StandardCharsets

/** The failure states of an `/enroll` answer; none of them carries a retry hint. */
public enum class EnrolFailure {
    /** A `200` whose body is not exactly the three keys with valid values. */
    BAD_ANSWER,

    /** `400`. */
    BAD_REQUEST,

    /** `404`: "enrolment refused - ask the operator" (decline, timeout or another refusal). */
    REFUSED,

    /** `413`. */
    TOO_LARGE,

    /** `429`. */
    RATE_LIMITED,

    /** `503`. */
    UNAVAILABLE,

    /** Any other status. */
    UNEXPECTED_STATUS,
}

/** The answer body, read by [EnrolAnswer.of] for a `200` only. */
public fun interface EnrolAnswerBody {
    /** Returns at most [limit] bytes of the body (all of it when it is shorter). */
    public fun read(limit: Int): ByteArray
}

/** Where [EnrolAnswer.of] logs: one fixed code per answer, never body text, token or certificate. */
public fun interface EnrolAnswerLog {
    public fun code(code: String)
}

/**
 * The outcome of the `/enroll` answer (`protocol/README.md` "HTTP `/enroll`"; T1.23c): the phone acts only on the
 * status code and, on `200`, on exactly `cert`, `token` and `deviceId`.
 */
public sealed interface EnrolAnswer {
    /** The issued certificate (one PEM block), the device token and the device id. */
    public class Issued internal constructor(
        public val certPem: String,
        public val token: String,
        public val deviceId: String,
    ) : EnrolAnswer {
        /** Leaves the token out of every log line. */
        override fun toString(): String = "Issued(deviceId=$deviceId)"
    }

    /** A failure state: only its [kind]. */
    public data class Failed(
        public val kind: EnrolFailure,
    ) : EnrolAnswer

    public companion object {
        /** Bound of a `200` body in bytes. */
        public const val MAX_BODY_BYTES: Int = 8192
        private const val OK = 200
        private val statusKinds =
            mapOf(
                400 to EnrolFailure.BAD_REQUEST,
                404 to EnrolFailure.REFUSED,
                413 to EnrolFailure.TOO_LARGE,
                429 to EnrolFailure.RATE_LIMITED,
                503 to EnrolFailure.UNAVAILABLE,
            )
        private val answerKeys = setOf("cert", "token", "deviceId")
        private val tokenShape = Regex("[A-Za-z0-9_-]{1,256}")
        private val deviceIdShape = Regex("[0-9a-f]{16}")
        private val pemShape =
            Regex("-----BEGIN CERTIFICATE-----\n((?:[A-Za-z0-9+/=]+\n)+)-----END CERTIFICATE-----\n?")

        /**
         * Maps [status] and, for a `200` only, [body]; logs one code to [log]. Any answer maps to a state; only an
         * exception thrown by [body] itself reaches the caller.
         */
        public fun of(
            status: Int,
            body: EnrolAnswerBody,
            log: EnrolAnswerLog,
        ): EnrolAnswer {
            val kind = statusKinds[status] ?: EnrolFailure.UNEXPECTED_STATUS
            val answer = if (status == OK) issued(body) else Failed(kind)
            val outcome = if (answer is Failed) answer.kind.name else "ISSUED"
            log.code("enrol_answer status=$status outcome=$outcome")
            return answer
        }

        private fun issued(body: EnrolAnswerBody): EnrolAnswer {
            val fields = answerObject(body.read(MAX_BODY_BYTES + 1))?.takeIf { it.keys == answerKeys }
            val cert = text(fields, "cert")?.takeIf(::onePemCertificate)
            val token = text(fields, "token")?.takeIf(tokenShape::matches)
            val deviceId = text(fields, "deviceId")?.takeIf(deviceIdShape::matches)
            return if (cert != null && token != null && deviceId != null) {
                Issued(cert, token, deviceId)
            } else {
                Failed(EnrolFailure.BAD_ANSWER)
            }
        }

        /** The body as one strict JSON object, or null when it is over the bound, not UTF-8 or not strict JSON. */
        private fun answerObject(bytes: ByteArray): JsonObject? {
            val text = if (bytes.size > MAX_BODY_BYTES) null else utf8(bytes)
            return (text?.let { StrictJson.readObject(it, MAX_BODY_BYTES, 1) } as? StrictJsonResult.Read)?.value
        }

        private fun utf8(bytes: ByteArray): String? =
            try {
                StandardCharsets.UTF_8
                    .newDecoder()
                    .decode(ByteBuffer.wrap(bytes))
                    .toString()
            } catch (expected: CharacterCodingException) {
                null
            }

        private fun text(
            fields: JsonObject?,
            key: String,
        ): String? = (fields?.get(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

        /** Exactly one `CERTIFICATE` block, `\n` line ends, whose base64 is canonical padded base64 of 1+ bytes. */
        private fun onePemCertificate(pem: String): Boolean {
            val lines = pemShape.matchEntire(pem)?.groupValues?.get(1)
            return lines != null && canonicalBase64OrNull(lines.replace("\n", "")) != null
        }
    }
}
