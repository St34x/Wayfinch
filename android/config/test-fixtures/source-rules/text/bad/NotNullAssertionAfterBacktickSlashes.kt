// SPDX-License-Identifier: Apache-2.0
// expect: NotNullAssertion
// A backtick name holding `//` is not a comment: the `!!` after it is code.

fun `name//with-slashes`(): String = System.getenv("HOME")!!
