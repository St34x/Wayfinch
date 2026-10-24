// SPDX-License-Identifier: Apache-2.0
// expect: NotNullAssertion
// The expression of a template in a raw string is code as well.

fun home(): String = """${System.getenv("HOME")!!}"""
