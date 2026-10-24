// SPDX-License-Identifier: Apache-2.0

fun nested3(a: Boolean, b: Boolean, c: Boolean): Int {
    var total = 0
    if (a) {
        if (b) {
            if (c) {
                total += 1
            }
        }
    }
    return total
}
