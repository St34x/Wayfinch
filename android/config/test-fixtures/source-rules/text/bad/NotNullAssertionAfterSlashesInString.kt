// SPDX-License-Identifier: Apache-2.0
// expect: NotNullAssertion
// A `//` inside a string literal is not a comment: the `!!` after it is code.

fun home(): String = "https://example.com/" + System.getenv("HOME")!!
