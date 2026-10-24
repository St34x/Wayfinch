// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.protocol.json

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/** Why [StrictJson.readObject] refused a text. Only for tests and log codes: it never carries input text. */
public enum class JsonRefusal {
    /** More UTF-8 bytes than the caller's bound. */
    TOO_LARGE,

    /** A leading byte order mark. */
    BOM,

    /** Not one JSON text (also `NaN`, `Infinity`, a raw control character, a bad escape). */
    SYNTAX,

    /** A complete JSON text followed by more than whitespace. */
    TRAILING,

    /** One JSON text, but not an object. */
    NOT_AN_OBJECT,

    /** A number that does not fit a finite double (`1e999`). */
    NON_FINITE_NUMBER,

    /** The same key twice in one object, compared after unescaping. */
    DUPLICATE_KEY,

    /** Objects and arrays nested deeper than the caller's bound. */
    TOO_DEEP,
}

/** The outcome of [StrictJson.readObject]: exactly one of the two. */
public sealed interface StrictJsonResult {
    /** The text was one JSON object without any defect. */
    public data class Read(
        public val value: JsonObject,
    ) : StrictJsonResult

    /** The text was refused; nothing of it is returned. */
    public data class Refused(
        public val reason: JsonRefusal,
    ) : StrictJsonResult
}

/**
 * The strict JSON object reader for inputs that are not protocol frames (the enrolment payload, the `/enroll` answer,
 * the `403` marker body; T1.23a). The generated envelope decoder keeps its own checks. Refuses everything listed in
 * [JsonRefusal]; never throws to the caller. An unpaired surrogate in a string (raw or `\uD800`) is read as it is, as
 * RFC 8259 allows: a caller shape-checks every string it uses before it shows or re-encodes it.
 */
public object StrictJson {
    /**
     * Reads [text] (at most [maxBytes] UTF-8 bytes, at most [maxDepth] nested objects and arrays) as one object. The
     * reader recurses once per nesting level, so callers pass a small fixed [maxDepth] (the enrolment payload uses 2).
     */
    public fun readObject(
        text: String,
        maxBytes: Int,
        maxDepth: Int,
    ): StrictJsonResult =
        when {
            utf8Length(text, maxBytes) > maxBytes -> StrictJsonResult.Refused(JsonRefusal.TOO_LARGE)
            text.startsWith(BOM) -> StrictJsonResult.Refused(JsonRefusal.BOM)
            else -> read(text, maxDepth)
        }

    private fun read(
        text: String,
        maxDepth: Int,
    ): StrictJsonResult =
        try {
            StrictJsonResult.Read(JsonReader(text, maxDepth).document())
        } catch (refusal: JsonRefused) {
            StrictJsonResult.Refused(refusal.reason)
        }

    /**
     * The UTF-8 length of [text], counted per code point; stops early once it is above [bound]. A lone surrogate counts
     * three bytes (its WTF-8 length), so no text is counted shorter than it is.
     */
    internal fun utf8Length(
        text: String,
        bound: Int,
    ): Int {
        var bytes = 0
        var index = 0
        while (index < text.length && bytes <= bound) {
            val point = text.codePointAt(index)
            bytes += utf8Width(point)
            index += Character.charCount(point)
        }
        return bytes
    }

    private fun utf8Width(point: Int): Int =
        when {
            point < ONE_BYTE_LIMIT -> 1
            point < TWO_BYTE_LIMIT -> 2
            point < Character.MIN_SUPPLEMENTARY_CODE_POINT -> THREE_BYTES
            else -> FOUR_BYTES
        }

    private const val BOM = "\uFEFF"
    private const val ONE_BYTE_LIMIT = 0x80
    private const val TWO_BYTE_LIMIT = 0x800
    private const val THREE_BYTES = 3
    private const val FOUR_BYTES = 4
}

/** Internal control flow of [JsonReader]; caught in [StrictJson.readObject] and never seen by a caller. */
internal class JsonRefused(
    val reason: JsonRefusal,
) : RuntimeException(null, null, false, false) {
    private companion object {
        private const val serialVersionUID: Long = 1
    }
}

/** A recursive-descent reader over the JSON grammar of RFC 8259; recursion is bounded by [maxDepth]. */
private class JsonReader(
    text: String,
    private val maxDepth: Int,
) {
    private val cursor = JsonCursor(text)
    private var depth = 0

    fun document(): JsonObject {
        cursor.skipSpace()
        val value = value()
        cursor.skipSpace()
        if (!cursor.atEnd()) fail(JsonRefusal.TRAILING)
        return value as? JsonObject ?: fail(JsonRefusal.NOT_AN_OBJECT)
    }

    private fun value(): JsonElement =
        when (cursor.peek()) {
            '{' -> nested { obj() }
            '[' -> nested { array() }
            '"' -> JsonPrimitive(cursor.string())
            't' -> cursor.literal("true", JsonPrimitive(true))
            'f' -> cursor.literal("false", JsonPrimitive(false))
            'n' -> cursor.literal("null", JsonNull)
            else -> cursor.number()
        }

    private fun nested(read: () -> JsonElement): JsonElement {
        depth += 1
        if (depth > maxDepth) fail(JsonRefusal.TOO_DEEP)
        cursor.next()
        cursor.skipSpace()
        return read().also { depth -= 1 }
    }

    private fun obj(): JsonObject {
        val members = LinkedHashMap<String, JsonElement>()
        while (!cursor.consume('}')) {
            if (members.isNotEmpty()) cursor.expect(',')
            cursor.skipSpace()
            if (cursor.peek() != '"') fail(JsonRefusal.SYNTAX)
            val key = cursor.string()
            cursor.skipSpace()
            cursor.expect(':')
            cursor.skipSpace()
            if (members.put(key, value()) != null) fail(JsonRefusal.DUPLICATE_KEY)
            cursor.skipSpace()
        }
        return JsonObject(members)
    }

    private fun array(): JsonArray {
        val items = ArrayList<JsonElement>()
        while (!cursor.consume(']')) {
            if (items.isNotEmpty()) cursor.expect(',')
            cursor.skipSpace()
            items.add(value())
            cursor.skipSpace()
        }
        return JsonArray(items)
    }
}

/** The tokens of RFC 8259: strings, literals and numbers (no `+`, leading zeros, `NaN`, `Infinity`). */
private class JsonCursor(
    private val text: String,
) {
    private var pos = 0

    fun atEnd(): Boolean = pos == text.length

    fun peek(): Char = if (pos < text.length) text[pos] else fail(JsonRefusal.SYNTAX)

    fun next(): Char = peek().also { pos += 1 }

    fun consume(char: Char): Boolean = (pos < text.length && text[pos] == char).also { if (it) pos += 1 }

    fun expect(char: Char) {
        if (!consume(char)) fail(JsonRefusal.SYNTAX)
    }

    fun skipSpace() {
        while (pos < text.length && text[pos] in JSON_SPACE) pos += 1
    }

    fun literal(
        word: String,
        value: JsonElement,
    ): JsonElement {
        if (!text.startsWith(word, pos)) fail(JsonRefusal.SYNTAX)
        pos += word.length
        return value
    }

    /** A number as a [Long] when it is an integer that fits, else as a finite double. */
    fun number(): JsonPrimitive {
        val token = NUMBER.matchAt(text, pos)?.value ?: fail(JsonRefusal.SYNTAX)
        pos += token.length
        val whole = if (token.none { it in ".eE" }) token.toLongOrNull() else null
        val double = token.toDouble()
        if (!double.isFinite()) fail(JsonRefusal.NON_FINITE_NUMBER)
        return if (whole != null) JsonPrimitive(whole) else JsonPrimitive(double)
    }

    /** A string at the cursor, unescaped; a raw control character or an unknown escape is refused. */
    fun string(): String {
        val out = StringBuilder()
        pos += 1
        while (true) {
            val char = next()
            when {
                char == '"' -> return out.toString()
                char == '\\' -> out.append(escape())
                char < ' ' -> fail(JsonRefusal.SYNTAX)
                else -> out.append(char)
            }
        }
    }

    private fun escape(): Char {
        val char = next()
        if (char != 'u') return ESCAPED.getOrNull(ESCAPES.indexOf(char)) ?: fail(JsonRefusal.SYNTAX)
        val hex = text.substring(pos, minOf(pos + HEX_DIGITS, text.length))
        if (hex.length != HEX_DIGITS || !hex.all { it in HEX_CHARS }) fail(JsonRefusal.SYNTAX)
        pos += HEX_DIGITS
        return hex.toInt(HEX_RADIX).toChar()
    }

    private companion object {
        const val HEX_DIGITS = 4
        const val HEX_RADIX = 16
        const val HEX_CHARS = "0123456789abcdefABCDEF"
        const val JSON_SPACE = " \t\n\r"
        const val ESCAPES = "\"\\/bfnrt"
        const val ESCAPED = "\"\\/\b\u000C\n\r\t"
        val NUMBER = Regex("-?(0|[1-9][0-9]*)(\\.[0-9]+)?([eE][+-]?[0-9]+)?")
    }
}

private fun fail(reason: JsonRefusal): Nothing = throw JsonRefused(reason)
