// SPDX-License-Identifier: Apache-2.0
// expect: ForbiddenImport

import android.util.Log

fun log(message: String) {
    Log.d("tag", message)
}
