// SPDX-License-Identifier: Apache-2.0
// expect: UnsafeCallOnNullableType

fun length(text: String?): Int = text!!.length
