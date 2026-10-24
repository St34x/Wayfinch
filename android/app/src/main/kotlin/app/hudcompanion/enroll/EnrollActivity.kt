// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.enroll

import android.app.Activity
import android.os.Bundle
import android.view.WindowManager

/**
 * Enrolment entry point slot (`wayfinch://enroll`). Placeholder until T1.23b: the window is secure from the first
 * frame (FLAG_SECURE: the enrolment payload is a screen secret), it reads nothing it was started with and closes.
 */
class EnrollActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        window.setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE)
        super.onCreate(savedInstanceState)
        finish()
    }
}
