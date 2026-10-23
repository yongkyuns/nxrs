package org.nxrs.monitor

import android.os.SystemClock
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.nxrs.monitor.core.*
import org.nxrs.monitor.media.*

data class CameraPresentation(
    val status: String = "Paused",
    val live: Boolean = false,
    val peer: NativePeer? = null,
    val presence: Presence = Presence.Unavailable,
    val historyIncomplete: Boolean = false,
    val fps: Int = 0,
    val width: Int = 0,
    val height: Int = 0,
    val error: String? = null,
    val recording: Boolean = false,
    val captureBusy: Boolean = false,
    val captureMessage: String? = null,
)

/**
 * Main-thread owner of exactly one camera and one immutable enrollment. Its admission slot survives
 * cancellation until remote/native cleanup has joined.
 */
class CameraSession(
    var camera: Camera,
    private val home: Enrollment,
    private val network: Network,
    private val runtime: MediaRuntime,
    private val library: CaptureLibrary,
    private val scope: CoroutineScope,
    private val released: () -> Unit,
) {
    private val mutable = MutableStateFlow(CameraPresentation())
    val state = mutable.asStateFlow()
    @Volatile
    var active = false
        private set

    @Volatile
    var retiring = false
        private set

    @Volatile
    var blocked = false
        private set

    val occupiesSlot
        get() = active || retiring

    private var task: Job? = null
    private var peer: NativePeer? = null
    private var failedRetirement = false

    suspend fun awaitTermination() {
        task?.join()
    }

    private var photo: Job? = null
    private var preparation: Job? = null
    private var finalization: Job? = null
    private var recording: Pair<NativeClipRecorder, java.io.File>? = null

    fun snapshot() {
        val native = peer ?: return
        if (!mutable.value.live || photo?.isActive == true) return
        mutable.value = mutable.value.copy(captureBusy = true, captureMessage = null)
        photo =
            scope.launch {
                try {
                    val bytes = withTimeout(3000) { NativeSnapshot(native).jpeg() }
                    library.photo(bytes, camera)
                    mutable.value = mutable.value.copy(captureMessage = "Snapshot saved")
                } catch (_: Exception) {
                    mutable.value =
                        mutable.value.copy(
                            captureMessage =
                                "Snapshot could not be saved. Check live video and available storage."
                        )
                } finally {
                    mutable.value = mutable.value.copy(captureBusy = false)
                    photo = null
                }
            }
    }

    fun toggleRecording() {
        if (recording != null) {
            endRecording()
            return
        }
        val native = peer ?: return
        if (!mutable.value.live || preparation?.isActive == true || finalization?.isActive == true)
            return
        preparation =
            scope.launch {
                var staging: java.io.File? = null
                try {
                    staging = library.reserve()
                    checked(active && peer === native && !native.isClosed, Failure.Unavailable)
                    recording = NativeClipRecorder(native, staging) to staging
                    mutable.value =
                        mutable.value.copy(
                            recording = true,
                            captureMessage = "Recording · maximum 60 seconds",
                        )
                } catch (_: Exception) {
                    staging?.let { library.discard(it) }
                    mutable.value =
                        mutable.value.copy(
                            captureMessage = "Recording could not start. Check available storage."
                        )
                } finally {
                    preparation = null
                }
            }
    }

    private fun endRecording() {
        val current = recording ?: return
        recording = null
        mutable.value = mutable.value.copy(recording = false, captureMessage = "Saving clip…")
        finalization =
            scope.launch {
                try {
                    val result = current.first.finish()
                    library.finish(current.second, result.durationMs, camera)
                    mutable.value = mutable.value.copy(captureMessage = "Clip saved")
                } catch (_: Exception) {
                    library.discard(current.second)
                    mutable.value =
                        mutable.value.copy(
                            captureMessage = "Clip could not be saved. Record at least two frames."
                        )
                } finally {
                    finalization = null
                }
            }
    }

    internal var detectionFailure: Failure? = null
        private set

    internal val detectionAvailable
        get() = detections.snapshot != null

    private val detections = DetectionState()
    private val cursor = AtomicReference<String?>(null)

    fun retry() {
        if (!failedRetirement) blocked = false
    }

    fun start() {
        if (occupiesSlot || blocked) return
        active = true
        task =
            scope.launch {
                try {
                    coroutineScope {
                        val feed = launch { receiveDetections() }
                        try {
                            playback()
                        } finally {
                            feed.cancelAndJoin()
                        }
                    }
                } finally {
                    active = false
                    retiring = true
                    endRecording()
                    withContext(NonCancellable) {
                        preparation?.join()
                        endRecording()
                        photo?.join()
                        finalization?.join()
                        if (peer?.closeAndJoin() == false) failedRetirement = true
                    }
                    peer = null
                    detections.reset()
                    cursor.set(null)
                    mutable.value =
                        mutable.value.copy(
                            peer = null,
                            live = false,
                            presence = Presence.Unavailable,
                        )
                    retiring = failedRetirement
                    if (failedRetirement) {
                        blocked = true
                        mutable.value =
                            CameraPresentation(
                                status = "Cleanup failed",
                                error = "Restart the app to release this camera's native resources.",
                            )
                    }
                    task = null
                    released()
                }
            }
    }

    fun stop() {
        active = false
        retiring = failedRetirement || task != null
        endRecording()
        peer?.close()
        task?.cancel()
        detections.reset()
        cursor.set(null)
        if (!failedRetirement) mutable.value = CameraPresentation()
    }

    private suspend fun receiveDetections() {
        var failures = 0
        while (currentCoroutineContext().isActive) {
            val started = SystemClock.elapsedRealtime()
            try {
                DetectionFeed(network)
                    .subscribe(home, camera.id, cursor.get(), cursor::set)
                    .collect { message ->
                        detections.receive(
                            message.snapshot,
                            camera.id,
                            message.received,
                            message.started,
                        )
                        detectionFailure = null
                    }
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                detections.reset()
                detectionFailure = (error as? ClientException)?.failure ?: Failure.Network
                if (detectionFailure in setOf(Failure.Unauthorized, Failure.Forbidden)) {
                    blocked = true
                    stop()
                    mutable.value =
                        CameraPresentation(status = "Access denied", error = message(error))
                    return
                }
                // Media and detection delivery have independent freshness. A
                // broken detection contract terminates only this subscription.
                if (fatal(error) || detectionFailure == Failure.TooLarge) return
            }
            detections.reset()
            failures = if (SystemClock.elapsedRealtime() - started >= 3000) 0 else failures + 1
            if (failures >= 4) return
            delay((1L shl (failures + 1).coerceAtMost(3)) * 1000)
        }
    }

    private suspend fun playback() {
        val api = GatewayClient(home.policy, network)
        val whep = WhepClient(home.policy, network)
        for (attempt in 0..3) {
            currentCoroutineContext().ensureActive()
            if (attempt > 0) {
                mutable.value = CameraPresentation(status = "Reconnecting · $attempt of 3")
                delay((1L shl attempt) * 1000)
            }
            val native = NativePeer(runtime)
            peer = native
            mutable.value = CameraPresentation(status = "Connecting", peer = native)
            var remote: WhepSession? = null
            var failure: Exception? = null
            try {
                val deadline = SystemClock.elapsedRealtime() + 15000
                fun remaining() = (deadline - SystemClock.elapsedRealtime()).coerceAtLeast(1)
                withStartupBudget(
                    abort = {
                        native.close()
                        mutable.value =
                            mutable.value.copy(
                                peer = null,
                                live = false,
                                presence = Presence.Unavailable,
                                status = "Connection interrupted",
                            )
                    }
                ) {
                    val offer = native.offer()
                    val grant = api.grant(camera.id, home.credential, remaining())
                    remote = whep.open(grant, offer, remaining())
                    native.answer(remote!!.answer)
                    while (native.frames.count == 0L) {
                        checked(!native.failed, Failure.Unavailable)
                        delay(50)
                    }
                }
                var sampleAt = SystemClock.elapsedRealtime()
                var count = 0L
                var fps = 0
                while (true) {
                    currentCoroutineContext().ensureActive()
                    val now = SystemClock.elapsedRealtime()
                    val frames = native.frames
                    val age = now - frames.arrivedAt
                    checked(!native.failed && age in 0..6499, Failure.Unavailable)
                    val live = age < 1500
                    if (now - sampleAt >= 1000) {
                        fps = ((frames.count - count) * 1000 / (now - sampleAt)).toInt()
                        count = frames.count
                        sampleAt = now
                    }
                    val presence =
                        if (live && frames.metadata != null) frames.metadata.presence(age)
                        else detections.presence(now, live)
                    if (!live || recording?.first?.isFinishing == true) endRecording()
                    val captures = mutable.value
                    mutable.value =
                        CameraPresentation(
                            if (live) "Live" else "Video interrupted",
                            live,
                            native,
                            presence,
                            detections.eventGap || detectionFailure != null,
                            fps,
                            frames.width,
                            frames.height,
                            recording = captures.recording,
                            captureBusy = captures.captureBusy,
                            captureMessage = captures.captureMessage,
                        )
                    delay(250)
                }
            } catch (error: Exception) {
                failure = error
            } finally {
                endRecording()
                native.close()
                if (peer === native) peer = null
                mutable.value =
                    mutable.value.copy(peer = null, live = false, presence = Presence.Unavailable)
                withContext(NonCancellable) {
                    remote?.let { whep.close(it) }
                    if (!native.closeAndJoin()) failedRetirement = true
                }
            }
            if (failedRetirement) return
            currentCoroutineContext().ensureActive()
            if (fatal(failure) || attempt == 3) {
                blocked = true
                mutable.value =
                    CameraPresentation(
                        status = if (fatal(failure)) "Access denied" else "Offline",
                        error = message(failure),
                    )
                return
            }
        }
    }

    companion object {
        fun fatal(error: Exception?) =
            (error as? ClientException)?.failure in
                setOf(
                    Failure.Unauthorized,
                    Failure.Forbidden,
                    Failure.UntrustedOrigin,
                    Failure.InvalidEndpoint,
                    Failure.Contract,
                    Failure.NotFound,
                    Failure.Busy,
                    Failure.Redirect,
                )

        fun message(error: Exception?) =
            when ((error as? ClientException)?.failure) {
                Failure.Unauthorized,
                Failure.Forbidden ->
                    "This viewer key cannot access this camera. Ask the home owner for a new invitation."
                Failure.UntrustedOrigin,
                Failure.InvalidEndpoint,
                Failure.Redirect -> "The video address is not trusted. Check the home invitation."
                Failure.NotFound -> "This camera is no longer shared with your viewer key."
                Failure.Busy -> "Your home is busy. Wait a moment, then retry."
                else ->
                    "The camera could not be reached. Check its certificate, power and private-network connection."
            }
    }
}
