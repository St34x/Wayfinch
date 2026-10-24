// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.enroll

import app.hudcompanion.protocol.json.JsonRefusal
import app.hudcompanion.protocol.json.StrictJson
import app.hudcompanion.protocol.json.StrictJsonResult
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.StandardCharsets
import java.util.Base64

/** Why [EnrolmentPayload.parse] refused an input: for tests and a log code only, never with input text. */
public enum class EnrolmentRefusal {
    TOO_LARGE,
    BAD_LINK,
    BAD_FRAGMENT,
    BAD_JSON,
    BAD_KEYS,
    BAD_VERSION,
    BAD_URL,
    BAD_HOST,
    BAD_CODE,
    BAD_PINS,
}

/**
 * The enrolment payload (T1.23a): the one strict parser for the app link
 * `wayfinch://enroll#<base64url JSON>` and for pasted text that is that link or the JSON itself. Every refusal shows
 * the user the same message (a string resource of the app); the reason is for tests and a log code.
 */
public sealed interface EnrolmentPayload {
    /** A valid payload: [origin] to post to, [host] for the "Enrol at <host>" button, the one-time [code], [pins]. */
    public data class Parsed(
        public val origin: String,
        public val host: String,
        public val code: String,
        public val pins: List<String>,
    ) : EnrolmentPayload {
        /** Leaves the one-time code out of every log line. */
        override fun toString(): String = "Parsed(origin=$origin, pins=${pins.size})"
    }

    /** No partial result: only the [reason] and, for [EnrolmentRefusal.BAD_JSON], the reader's [json] reason. */
    public data class Refused(
        public val reason: EnrolmentRefusal,
        public val json: JsonRefusal? = null,
    ) : EnrolmentPayload

    public companion object {
        /** Input bound in UTF-8 bytes, counted on the raw input before anything is trimmed or decoded. */
        public const val MAX_BYTES: Int = 2048
        private const val MAX_DEPTH = 2
        private const val LINK_PREFIX = "wayfinch://enroll#"
        private const val PIN_PREFIX = "sha256/"
        private const val PIN_BYTES = 32
        private const val MAX_PINS = 8
        private const val ASCII_SPACE = " \t\n\r\u000C"
        private val payloadKeys = setOf("version", "url", "code", "pins")
        private val base64url = Regex("[A-Za-z0-9_-]+")

        /** Parses an app link or pasted text; never throws. */
        public fun parse(input: String): EnrolmentPayload =
            if (StrictJson.utf8Length(input, MAX_BYTES) > MAX_BYTES) {
                Refused(EnrolmentRefusal.TOO_LARGE)
            } else {
                try {
                    fields(readJson(input.trim { it in ASCII_SPACE }))
                } catch (refusal: EnrolmentRefused) {
                    Refused(refusal.reason, refusal.json)
                }
            }

        private fun readJson(text: String): JsonObject {
            val json = if (text.startsWith("wayfinch:")) linkJson(text) else text
            return when (val read = StrictJson.readObject(json, MAX_BYTES, MAX_DEPTH)) {
                is StrictJsonResult.Read -> read.value
                is StrictJsonResult.Refused -> throw EnrolmentRefused(EnrolmentRefusal.BAD_JSON, read.reason)
            }
        }

        private fun linkJson(link: String): String {
            if (!link.startsWith(LINK_PREFIX)) refusePayload(EnrolmentRefusal.BAD_LINK)
            val fragment = link.substring(LINK_PREFIX.length)
            if (!base64url.matches(fragment)) refusePayload(EnrolmentRefusal.BAD_FRAGMENT)
            val bytes = decodeOrNull(fragment, Base64.getUrlDecoder()) ?: refusePayload(EnrolmentRefusal.BAD_FRAGMENT)
            if (Base64.getUrlEncoder().withoutPadding().encodeToString(bytes) != fragment) {
                refusePayload(EnrolmentRefusal.BAD_FRAGMENT)
            }
            return try {
                StandardCharsets.UTF_8
                    .newDecoder()
                    .decode(ByteBuffer.wrap(bytes))
                    .toString()
            } catch (expected: CharacterCodingException) {
                refusePayload(EnrolmentRefusal.BAD_FRAGMENT)
            }
        }

        private fun fields(payload: JsonObject): Parsed {
            if (payload.keys != payloadKeys) refusePayload(EnrolmentRefusal.BAD_KEYS)
            val version = payload.getValue("version") as? JsonPrimitive
            if (version == null || version.isString || version.content != "1") {
                refusePayload(EnrolmentRefusal.BAD_VERSION)
            }
            val origin = HttpsOrigin.parse(text(payload, "url") ?: refusePayload(EnrolmentRefusal.BAD_URL))
            val code =
                text(payload, "code")?.takeIf(enrolCodeShape::matches)
                    ?: refusePayload(EnrolmentRefusal.BAD_CODE)
            return Parsed(origin.origin, origin.host, code, pins(payload.getValue("pins")))
        }

        private fun pins(value: JsonElement): List<String> {
            val pins = (value as? JsonArray)?.map { (it as? JsonPrimitive)?.takeIf { pin -> pin.isString }?.content }
            if (pins == null || pins.size !in 1..MAX_PINS || pins.toSet().size != pins.size) {
                refusePayload(EnrolmentRefusal.BAD_PINS)
            }
            return pins.map { it?.takeIf(::canonicalPin) ?: refusePayload(EnrolmentRefusal.BAD_PINS) }
        }

        private fun canonicalPin(pin: String): Boolean =
            pin.startsWith(PIN_PREFIX) && canonicalBase64OrNull(pin.removePrefix(PIN_PREFIX))?.size == PIN_BYTES

        private fun text(
            payload: JsonObject,
            key: String,
        ): String? = (payload.getValue(key) as? JsonPrimitive)?.takeIf { it.isString }?.content

        private fun decodeOrNull(
            text: String,
            decoder: Base64.Decoder,
        ): ByteArray? =
            try {
                decoder.decode(text)
            } catch (expected: IllegalArgumentException) {
                null
            }
    }
}

/** Internal control flow of [EnrolmentPayload.parse]; caught there and never seen by a caller. */
internal class EnrolmentRefused(
    val reason: EnrolmentRefusal,
    val json: JsonRefusal? = null,
) : RuntimeException(null, null, false, false) {
    private companion object {
        private const val serialVersionUID: Long = 1
    }
}

internal fun refusePayload(reason: EnrolmentRefusal): Nothing = throw EnrolmentRefused(reason)
