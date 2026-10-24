// SPDX-License-Identifier: Apache-2.0
// expect: NotNullAssertion
// A raw string may end in a quote (four quotes close it): the `!!` after it is code, not part of a second string.

fun home(): String = """a"""" + System.getenv("HOME")!! + "c"
