// SPDX-License-Identifier: Apache-2.0

fun run(action: () -> Unit) {
    try {
        action()
    } catch (e: IllegalStateException) {
        throw IllegalArgumentException(e)
    }
}
