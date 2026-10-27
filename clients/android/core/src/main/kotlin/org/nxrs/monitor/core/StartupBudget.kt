package org.nxrs.monitor.core

import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.*

/**
 * Abort the local resource at the deadline, before joining a noncancellable allocation-response
 * drain or remote cleanup. The caller still closes locally before cancelling for an explicit stop.
 * Every child is joined.
 */
suspend fun <T> withStartupBudget(
    timeoutMs: Long = 15000,
    abort: () -> Unit,
    start: suspend () -> T,
): T = coroutineScope {
    checked(timeoutMs in 1..60000)
    val aborted = AtomicBoolean(false)
    fun abortOnce() {
        if (aborted.compareAndSet(false, true)) abort()
    }
    val timer =
        launch(start = CoroutineStart.UNDISPATCHED) {
            delay(timeoutMs)
            abortOnce()
        }
    try {
        withTimeout(timeoutMs) { start() }
    } catch (error: Exception) {
        abortOnce()
        throw error
    } finally {
        timer.cancelAndJoin()
    }
}
