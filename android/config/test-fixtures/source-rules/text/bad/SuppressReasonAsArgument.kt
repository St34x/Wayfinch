// SPDX-License-Identifier: Apache-2.0
// expect: SuppressWithoutReason
// An extra suppression name that starts with `//` is no reason.

@Suppress("unused", "//reason")
fun quiet() = Unit
