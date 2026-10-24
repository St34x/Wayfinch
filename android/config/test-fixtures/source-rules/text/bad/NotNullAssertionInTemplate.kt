// SPDX-License-Identifier: Apache-2.0
// expect: NotNullAssertion
// The expression of a string template is code: this `!!` on a JDK value is a not-null assertion.

fun home(): String = "home=${System.getenv("HOME")!!}"
