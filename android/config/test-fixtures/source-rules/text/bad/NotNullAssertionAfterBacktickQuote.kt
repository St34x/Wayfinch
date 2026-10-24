// SPDX-License-Identifier: Apache-2.0
// expect: NotNullAssertion
// A backtick name holding a quote does not open a string: the `!!` after it is code.

fun `q"1`(x: String?): String = x!! + "y"
