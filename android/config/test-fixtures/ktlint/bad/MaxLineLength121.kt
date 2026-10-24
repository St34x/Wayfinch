// SPDX-License-Identifier: Apache-2.0
// expect: standard:max-line-length

fun text(suffix: String): String {
    val value = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
    return value + suffix
}
