// SPDX-License-Identifier: Apache-2.0
// expect: MaxLineLength

fun text(suffix: String): String {
    val value = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    return value + suffix
}
