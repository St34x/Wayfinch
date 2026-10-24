// SPDX-License-Identifier: Apache-2.0
// expect: GlobalCoroutineUsage

import kotlinx.coroutines.GlobalScope
import kotlinx.coroutines.launch

fun start(action: () -> Unit) {
    GlobalScope.launch { action() }
}
