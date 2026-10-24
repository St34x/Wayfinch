// SPDX-License-Identifier: Apache-2.0
package app.hudcompanion.core.revoke

/** The seven steps of the phone-side revocation wipe (`docs/architecture.md`); the ordinal is the step number. */
public enum class RevocationStep {
    PERSIST_FLAG,
    STOP_SENDING,
    STOP_TRAFFIC,
    WIPE_SECRETS,
    CLEAR_SHOWN,
    WIPE_CONTENT,
    AUDIT,
}

/** One component's part of a step. Idempotent: a re-run repeats it. */
public fun interface RevocationHook {
    public fun run()
}

/** Log port. Lines carry step numbers, hook and exception type names and counts, never content. */
public fun interface RevocationLog {
    public fun line(text: String)
}

/** A hook that threw: its step and the type names of the hook and the exception (never the message). */
public data class HookFailure(
    public val step: RevocationStep,
    public val hookType: String,
    public val errorType: String,
)

/** What one run did: its number (1 for the first) and every failed hook. */
public data class RevocationResult(
    public val run: Int,
    public val failures: List<HookFailure>,
) {
    /**
     * Step 0 failed: a step-0 hook threw, or after the step-0 hooks the flag was not set (or could not be read).
     * The caller must not rely on a re-run at the next start.
     */
    public val persistFailed: Boolean get() = failures.any { it.step == RevocationStep.PERSIST_FLAG }
}

/**
 * Runs the registered hooks step 0 to 6, several per step in registration order. Every hook is fail-safe: any
 * `Throwable` (an exception, a not-implemented stub, a missing class, any other `Error`) is logged by type and the
 * next hook runs, so no single component stops the wipe. After the step-0 hooks the sequence checks that the flag is
 * set and records a step-0 failure when it is not. A real process death throws nothing; the stored flag makes
 * [resumeAtStart] re-run the sequence at the next start. Hooks run while the sequence is locked: a hook must not
 * block on another thread or call back into the sequence.
 */
public class RevocationSequence(
    private val flag: RevokedFlag,
    private val log: RevocationLog,
) {
    private val hooks = RevocationStep.entries.associateWith { mutableListOf<RevocationHook>() }
    private var runs = 0

    /** Number of runs so far. */
    public val runCount: Int get() = synchronized(hooks) { runs }

    public fun register(
        step: RevocationStep,
        hook: RevocationHook,
    ) {
        synchronized(hooks) { hooks.getValue(step).add(hook) }
    }

    /** Runs steps 0 to 6 once. Concurrent signals are serialised: the later one repeats the (idempotent) hooks. */
    public fun run(): RevocationResult =
        synchronized(hooks) {
            runs++
            val failures = mutableListOf<HookFailure>()
            for (step in RevocationStep.entries) {
                hooks.getValue(step).toList().mapNotNullTo(failures) { runHook(step, it) }
                if (step == RevocationStep.PERSIST_FLAG) runHook(step, FlagStoredCheck())?.let(failures::add)
            }
            say("revoke run=$runs failed=${failures.size}")
            RevocationResult(runs, failures)
        }

    /**
     * Re-runs 0 to 6 when the flag is set (T1.24b calls this before link, modules or chat page start). Throws, and
     * runs no hook, when the flag cannot be read: the caller then must not connect (SAT-PL-64).
     */
    public fun resumeAtStart(): RevocationResult? = if (flag.isSet()) run() else null

    /**
     * Runs the sequence only when [revoked] is true. The caller passes exactly "the T1.24d classifier answered
     * `Revoked`" (close `4403` or a marked `403`); `AccessDenied` (every unmarked `403`) and `NotRevoked` pass
     * false. Never pass "not `NotRevoked`": an unmarked `403` would then wipe the phone.
     */
    public fun onSignal(revoked: Boolean): RevocationResult? = if (revoked) run() else null

    /** The step-0 invariant: after the step-0 hooks the flag must be stored. */
    private inner class FlagStoredCheck : RevocationHook {
        override fun run(): Unit = check(flag.isSet()) { "revoked flag not stored" }
    }

    @Suppress("TooGenericExceptionCaught") // the outer boundary of independent components: one failure stops no wipe
    private fun runHook(step: RevocationStep, hook: RevocationHook): HookFailure? =
        try {
            hook.run()
            null
        } catch (e: Throwable) {
            HookFailure(step, hook.javaClass.name, e.javaClass.name).also {
                say("revoke step=${step.ordinal} hook=${it.hookType} failed=${it.errorType}")
            }
        }

    @Suppress("TooGenericExceptionCaught") // reason: a failing log sink must not stop the wipe either
    private fun say(text: String) {
        try {
            log.line(text)
        } catch (_: Throwable) {
            // nothing to report to: the sink is the only reporting channel
        }
    }
}
