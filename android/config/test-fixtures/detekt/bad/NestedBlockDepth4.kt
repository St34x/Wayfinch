// SPDX-License-Identifier: Apache-2.0
// expect: NestedBlockDepth

fun nested4(a: Boolean, b: Boolean, c: Boolean, d: Boolean): Int {
    var total = 0
    if (a) {
        if (b) {
            if (c) {
                if (d) {
                    total += 1
                }
            }
        }
    }
    return total
}
