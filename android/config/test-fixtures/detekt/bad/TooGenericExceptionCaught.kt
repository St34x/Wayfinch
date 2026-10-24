// SPDX-License-Identifier: Apache-2.0
// expect: TooGenericExceptionCaught

fun run(action: () -> Unit) {
    try {
        action()
    } catch (e: Exception) {
        throw IllegalStateException(e)
    }
}
