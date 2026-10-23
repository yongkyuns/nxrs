package org.nxrs.monitor

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.nxrs.monitor.core.*
import org.nxrs.monitor.media.MediaRuntime

data class HomePresentation(
    val enrolled: Boolean = false,
    val loading: Boolean = false,
    val cameras: List<Camera> = emptyList(),
    val sessions: Map<CameraId, CameraSession> = emptyMap(),
    val selected: CameraId? = null,
    val grid: Boolean = true,
    val fullscreen: CameraId? = null,
    val paused: Set<CameraId> = emptySet(),
    val error: String? = null,
)

class HomeModel(application: Application) : AndroidViewModel(application) {
    private val app = application as MonitorApplication
    private val controller = app.controller
    private val lease = Any()
    val library
        get() = app.library

    val state = controller.state

    fun enroll(gateway: String, media: String, token: String) {
        app.scope.launch { controller.enroll(gateway, media, token) }
    }

    fun forget() {
        app.scope.launch { controller.forget() }
    }

    fun refresh() {
        controller.refresh()
    }

    fun foreground(value: Boolean) {
        app.foreground(lease, value)
    }

    fun visible(ids: Set<CameraId>) {
        controller.visible(ids)
    }

    fun grid(value: Boolean) {
        controller.grid(value)
    }

    fun select(id: CameraId) {
        controller.select(id)
    }

    fun toggle(id: CameraId) {
        controller.toggle(id)
    }

    fun fullscreen(id: CameraId?) {
        controller.fullscreen(id)
    }

    override fun onCleared() {
        app.foreground(lease, false)
    }
}

/** Catalog/enrollment owner; camera resources live only in CameraSession. */
class HomeController(
    private val store: EnrollmentStore,
    private val network: Network,
    private val runtime: MediaRuntime,
    private val library: CaptureLibrary,
    private val scope: CoroutineScope,
) {
    private val mutable = MutableStateFlow(HomePresentation())
    val state = mutable.asStateFlow()
    private var home: Enrollment? = null
    private var foreground = false
    private var visible = emptySet<CameraId>()
    private val retired = mutableListOf<CameraSession>()
    private var catalog: Job? = null
    private var identity = 0L
    private var enrolling = false

    suspend fun restore() {
        val ticket = identity
        try {
            val saved = withContext(Dispatchers.IO) { store.read() }
            if (ticket != identity || home != null) return
            if (saved != null) {
                home = saved
                mutable.value = HomePresentation(enrolled = true)
                refresh()
            }
        } catch (_: Exception) {
            mutable.value =
                HomePresentation(
                    error =
                        "The saved invitation could not be opened. Unlock your device or enter a new invitation."
                )
        }
    }

    suspend fun enroll(gateway: String, media: String, token: String) {
        if (enrolling) return
        enrolling = true
        try {
            val candidate =
                Enrollment(
                    EndpointPolicy.parse(gateway.trim()),
                    media.trim().takeIf { it.isNotEmpty() }?.let(EndpointPolicy::parse),
                    Credential.parse(token.trim()),
                )
            GatewayClient(candidate.policy, network).cameras(candidate.credential)
            withContext(Dispatchers.IO) { store.save(candidate) }
            retireAll()
            home = candidate
            mutable.value = HomePresentation(enrolled = true)
            refresh()
        } catch (error: CancellationException) {
            throw error
        } catch (_: Exception) {
            mutable.value =
                mutable.value.copy(
                    error =
                        "Enter an HTTPS home address, an optional trusted video origin and a valid viewer key."
                )
        } finally {
            enrolling = false
        }
    }

    suspend fun forget() {
        try {
            withContext(Dispatchers.IO) { store.forget() }
            retireAll()
            home = null
            mutable.value = HomePresentation()
        } catch (_: Exception) {
            mutable.value =
                mutable.value.copy(
                    error = "The invitation could not be removed. Unlock this device and try again."
                )
        }
    }

    fun foreground(value: Boolean) {
        if (foreground == value) return
        foreground = value
        if (value) refresh()
        else {
            catalog?.cancel()
            catalog = null
            mutable.value = mutable.value.copy(loading = false)
        }
        apply()
    }

    fun refresh() {
        val enrollment = home ?: return
        if (!foreground || catalog?.isActive == true) return
        val ticket = identity
        mutable.value = mutable.value.copy(loading = true, error = null)
        catalog =
            scope.launch {
                try {
                    val cameras =
                        GatewayClient(enrollment.policy, network).cameras(enrollment.credential)
                    if (ticket != identity) return@launch
                    val old = mutable.value.sessions
                    val sessions =
                        cameras.associate { camera ->
                            val existing = old[camera.id]
                            val reusable =
                                existing?.camera?.let {
                                    it.copy(
                                        name = camera.name,
                                        availability = camera.availability,
                                    ) == camera
                                } == true
                            camera.id to
                                if (reusable) existing!!.also { it.camera = camera }
                                else {
                                    if (existing != null) retire(existing)
                                    CameraSession(
                                        camera,
                                        enrollment,
                                        network,
                                        runtime,
                                        library,
                                        scope,
                                        ::released,
                                    )
                                }
                        }
                    for ((id, session) in old) if (id !in sessions) retire(session)
                    val current = mutable.value
                    mutable.value =
                        current.copy(
                            cameras = cameras,
                            sessions = sessions,
                            loading = false,
                            selected =
                                current.selected?.takeIf { it in sessions }
                                    ?: cameras.firstOrNull()?.id,
                            paused = current.paused.intersect(sessions.keys),
                        )
                    apply()
                } catch (error: CancellationException) {
                    throw error
                } catch (error: Exception) {
                    if (ticket == identity)
                        mutable.value =
                            mutable.value.copy(
                                loading = false,
                                error = CameraSession.message(error),
                            )
                } finally {
                    if (ticket == identity) catalog = null
                }
            }
    }

    fun visible(ids: Set<CameraId>) {
        visible = ids.intersect(mutable.value.sessions.keys)
        apply()
    }

    fun grid(value: Boolean) {
        mutable.value = mutable.value.copy(grid = value)
        apply()
    }

    fun select(id: CameraId) {
        if (id in mutable.value.sessions) {
            mutable.value = mutable.value.copy(selected = id)
            apply()
        }
    }

    fun fullscreen(id: CameraId?) {
        if (id == null || id in mutable.value.sessions) {
            mutable.value = mutable.value.copy(fullscreen = id)
            apply()
        }
    }

    fun retry(id: CameraId) {
        val session = mutable.value.sessions[id] ?: return
        session.stop()
        session.retry()
        mutable.value = mutable.value.copy(paused = mutable.value.paused - id)
        apply()
    }

    fun toggle(id: CameraId) {
        val current = mutable.value
        val session = current.sessions[id] ?: return
        val pause = id !in current.paused && session.occupiesSlot
        mutable.value =
            current.copy(
                paused = if (pause) current.paused + id else current.paused - id,
                selected = id,
            )
        if (!pause) session.retry()
        apply()
    }

    private fun requested(): List<CameraId> {
        val current = mutable.value
        if (!foreground) return emptyList()
        val desired =
            current.fullscreen?.let { listOf(it) }
                ?: if (current.grid) {
                    listOfNotNull(current.selected?.takeIf { it in visible }) +
                        current.cameras.map { it.id }.filter { it in visible }
                } else listOfNotNull(current.selected)
        return desired.distinct().filter {
            it !in current.paused && current.sessions[it]?.camera?.liveVideo == true
        }
    }

    private fun apply() {
        val sessions = mutable.value.sessions
        retired.removeAll { !it.occupiesSlot }
        val plan =
            ViewingPlan.make(
                requested(),
                sessions.filterValues { it.active }.keys,
                sessions.filterValues { it.retiring }.keys,
                sessions.filterValues { it.blocked }.keys,
                retired.count { it.occupiesSlot },
            )
        plan.stop.forEach { sessions[it]?.stop() }
        plan.start.forEach { sessions[it]?.start() }
    }

    private fun released() {
        retired.removeAll { !it.occupiesSlot }
        apply()
    }

    private fun retire(session: CameraSession) {
        if (session.occupiesSlot) {
            retired.add(session)
            session.stop()
        }
    }

    private fun retireAll() {
        identity++
        catalog?.cancel()
        catalog = null
        mutable.value.sessions.values.forEach(::retire)
        visible = emptySet()
    }

    fun close() {
        foreground = false
        retireAll()
        mutable.value = HomePresentation()
    }

    suspend fun awaitClosed() {
        for (session in retired.toList()) session.awaitTermination()
    }
}
