// SPDX-License-Identifier: Apache-2.0
// expect: NotNullAssertion
// detekt's UnsafeCallOnNullableType cannot type JDK values (its classpath is the Kotlin standard library), so the
// text rule is the gate for a `!!` on them.

fun parentName(path: String): String = java.io.File(path).parentFile!!.name
