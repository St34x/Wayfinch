// SPDX-License-Identifier: Apache-2.0
// expect: CyclomaticComplexMethod

fun branches(value: Int): Int {
    var total = 0
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    if (value > 0) total += 1
    return total
}
