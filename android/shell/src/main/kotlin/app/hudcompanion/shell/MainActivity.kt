// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.shell

import android.app.Activity
import android.os.Bundle

/** Launcher slot. Placeholder until T1.37a: it reads nothing it was started with and closes. */
public class MainActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        finish()
    }
}
