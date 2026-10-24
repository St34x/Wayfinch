// SPDX-License-Identifier: Apache-2.0
// expect: ForbiddenImport
// A static member import of android.util.Log is as banned as the class.

import android.util.Log.d

fun log(message: String): Int = d("tag", message)
