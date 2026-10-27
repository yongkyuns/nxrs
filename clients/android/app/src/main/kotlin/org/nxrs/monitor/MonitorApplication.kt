package org.nxrs.monitor

import android.app.Application
import kotlinx.coroutines.*
import org.nxrs.monitor.media.MediaRuntime

/**
 * One process-wide home/admission owner. Activity replacement cannot reset the decoder budget while
 * a previous view's native cleanup is still retiring.
 */
class MonitorApplication : Application() {
    internal val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    internal lateinit var controller: HomeController
        private set

    internal lateinit var library: CaptureLibrary
        private set

    private val foreground = mutableSetOf<Any>()

    override fun onCreate() {
        super.onCreate()
        library = CaptureLibrary(this)
        controller =
            HomeController(EnrollmentStore(this), Network(), MediaRuntime(this), library, scope)
        scope.launch { controller.restore() }
    }

    internal fun foreground(owner: Any, active: Boolean) {
        if (active) foreground.add(owner) else foreground.remove(owner)
        controller.foreground(foreground.isNotEmpty())
    }
}
