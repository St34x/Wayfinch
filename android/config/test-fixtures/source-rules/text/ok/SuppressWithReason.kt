// SPDX-License-Identifier: Apache-2.0

@Suppress("LongMethod") // reason: a flat table kept in one place
fun quiet() = Unit

@Suppress("unused") val url = "https://example.com" // reason: the comment after a string with slashes counts

@get:Suppress("unused") // reason: a use-site target
val target = 1

@[Suppress("unused")] // reason: the bracket form
val bracket = 2

@SuppressWarnings("unused") // reason: SuppressWarnings is a suppression as well
val warnings = 3
