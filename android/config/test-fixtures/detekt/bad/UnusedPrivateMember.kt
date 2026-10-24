// SPDX-License-Identifier: Apache-2.0
// expect: UnusedPrivateMember

private fun unused(value: Int): Int = value + 1

fun used(value: Int): Int = value
