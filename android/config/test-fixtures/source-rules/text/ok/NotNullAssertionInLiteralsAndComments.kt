// SPDX-License-Identifier: Apache-2.0
// A not-null assertion `value!!` or @Suppress("all") in a comment is text, not code.

/** KDoc: `value!!` and @Suppress("all") are text as well. */
val shout = "Hello!!"
val comment = "see https://example.com/ // not a comment, and not a suppression: @Suppress"
val raw =
    """
    wow!!
    @Suppress("all")
    """
val quote = '"' // a char literal that looks like the start of a string; `!!` here is a comment
val bang = '!'
val template = "a ${shout.length} ${"!!"} ${'"'}" // template expressions without a not-null assertion
val rawEndingInQuote = """wow!!"""" + "still a string!!"
val tail = 1

/* block comment with value!! and @file:Suppress("all") */
fun `name with !! and "quotes" // and slashes`(text: String?): Int = text?.length ?: 0
