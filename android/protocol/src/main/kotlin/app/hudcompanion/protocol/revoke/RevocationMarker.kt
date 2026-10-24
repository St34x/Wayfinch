// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.revoke

import app.hudcompanion.protocol.json.StrictJson
import app.hudcompanion.protocol.json.StrictJsonResult
import kotlinx.serialization.json.JsonPrimitive
import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.charset.CharacterCodingException
import java.nio.charset.StandardCharsets

/** What a failed upgrade or a close means for revocation. Only [Revoked] starts the wipe sequence. */
public enum class RevocationSignal {
    /** The server's revocation marker: close `4403`, or a marked `403`. */
    Revoked,

    /** A `403` without the marker: the link shows "access denied" and wipes nothing. */
    AccessDenied,

    /** Any other status or close code; the link maps it to its own state. */
    NotRevoked,
}

/**
 * The phone's reading of the server's revocation marker (`protocol/README.md`, close codes and upgrade statuses). A
 * `403` is [RevocationSignal.Revoked] only with media type `application/json` (ASCII case-insensitive; a character
 * above 0x7F refuses; the only parameter allowed is `charset=utf-8`) and a body of at most [MAX_BODY_BYTES] bytes that
 * [StrictJson] reads as one object whose `error` is the string `device_revoked`. A stream that fails with an
 * [IOException] or an [IllegalStateException] (a closed source) is an unmarked body; any other exception of the stream
 * propagates. Never logs or returns body text. The classifier does not close the body: the caller closes the response
 * on every branch, also `NotRevoked`, where the body is never read.
 */
public object RevocationMarker {
    /** The largest marked body; the reader stops after one byte more. */
    public const val MAX_BODY_BYTES: Int = 1024

    /** Classifies a refused upgrade. [body] is read only for a `403` with the JSON media type, at most 1 025 bytes. */
    public fun classifyUpgrade(
        status: Int,
        contentType: String?,
        body: InputStream,
    ): RevocationSignal =
        when {
            status != HTTP_FORBIDDEN -> RevocationSignal.NotRevoked
            isJsonUtf8(contentType) && isMarkedBody(body) -> RevocationSignal.Revoked
            else -> RevocationSignal.AccessDenied
        }

    /** Classifies a WebSocket close: only `4403` is a signal, whatever its reason text. */
    public fun classifyClose(code: Int): RevocationSignal =
        if (code == CLOSE_REVOKED) RevocationSignal.Revoked else RevocationSignal.NotRevoked

    private fun isJsonUtf8(contentType: String?): Boolean {
        // equalsIgnoreCase also folds U+0131, U+0130 and U+017F onto ASCII letters: refuse them first
        if (contentType != null && contentType.any { it.code > MAX_ASCII }) return false
        val parts = contentType?.split(';')?.map { it.trim(' ', '\t') }.orEmpty()
        val params = parts.drop(1)
        val paramsOk = params.isEmpty() || params.singleOrNull()?.equals(CHARSET_UTF8, ignoreCase = true) == true
        return parts.firstOrNull()?.equals(MEDIA_JSON, ignoreCase = true) == true && paramsOk
    }

    private fun isMarkedBody(body: InputStream): Boolean {
        val text = readCapped(body)?.let(::decodeUtf8)
        val read = text?.let { StrictJson.readObject(it, MAX_BODY_BYTES, MAX_DEPTH) } as? StrictJsonResult.Read
        val error = read?.value?.get(KEY_ERROR) as? JsonPrimitive
        return error != null && error.isString && error.content == MARKER
    }

    /** At most [MAX_BODY_BYTES] + 1 bytes; null for a longer body, a failing stream or a stream that reads 0. */
    private fun readCapped(body: InputStream): ByteArray? =
        try {
            readUpToCap(body)
        } catch (_: IllegalStateException) {
            null // a closed source
        }

    private fun readUpToCap(body: InputStream): ByteArray? {
        val buffer = ByteArray(MAX_BODY_BYTES + 1)
        var filled = 0
        var read = 1
        try {
            while (filled < buffer.size && read > 0) {
                read = body.read(buffer, filled, buffer.size - filled)
                if (read > 0) filled += read
            }
        } catch (_: IOException) {
            return null
        }
        // 0 for a non-empty request breaks the InputStream contract: no body to trust
        return if (read != 0 && filled <= MAX_BODY_BYTES) buffer.copyOf(filled) else null
    }

    private fun decodeUtf8(bytes: ByteArray): String? =
        try {
            StandardCharsets.UTF_8
                .newDecoder()
                .decode(ByteBuffer.wrap(bytes))
                .toString()
        } catch (_: CharacterCodingException) {
            null
        }

    private const val HTTP_FORBIDDEN = 403
    private const val CLOSE_REVOKED = 4403
    private const val MAX_DEPTH = 2
    private const val MAX_ASCII = 0x7F
    private const val MEDIA_JSON = "application/json"
    private const val CHARSET_UTF8 = "charset=utf-8"
    private const val KEY_ERROR = "error"
    private const val MARKER = "device_revoked"
}
