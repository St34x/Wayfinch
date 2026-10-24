// SPDX-License-Identifier: Apache-2.0
// expect: SuppressWithoutReason

class Quiet(@param:Suppress("unused") val value: Int)
