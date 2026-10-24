package org.nxrs.monitor.media

import android.content.Context
import android.os.SystemClock
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import org.nxrs.monitor.core.*
import org.webrtc.*

interface OwnedVideoSink : VideoSink {
    fun close()

    suspend fun awaitClosed()
}

internal enum class PeerFailure {
    FrameLimit,
    Render,
    Connectivity,
    Sink,
    Initialization,
}

class RenderContext
internal constructor(val value: EglBase.Context, private val release: () -> Unit) {
    private val closed = AtomicBoolean(false)

    fun close() {
        if (closed.compareAndSet(false, true)) release()
    }
}

/**
 * Shared EGL root only. Each admitted peer owns its decoder factory, SDK executor, metadata
 * namespace and sinks; no camera data lives in this root.
 */
class MediaRuntime(context: Context) {
    private var root: EglBase? = null
    private var leases = 0

    companion object {
        private var initialized = false
    }

    init {
        synchronized(MediaRuntime::class.java) {
            if (!initialized) {
                PeerConnectionFactory.initialize(
                    PeerConnectionFactory.InitializationOptions.builder(context.applicationContext)
                        .setInjectableLogger({ _, _, _ -> }, Logging.Severity.LS_NONE)
                        .createInitializationOptions()
                )
                initialized = true
            }
        }
    }

    @Synchronized
    fun acquire(): EglBase.Context {
        if (root == null) root = EglBase.create()
        leases++
        return root!!.eglBaseContext
    }

    @Synchronized
    fun release() {
        check(leases > 0)
        leases--
        if (leases == 0) {
            val previous = root
            root = null
            previous?.release()
        }
    }
}

data class DecodedProgress(
    val count: Long = 0,
    val width: Int = 0,
    val height: Int = 0,
    val arrivedAt: Long = 0,
    val metadata: VideoDetectionFrame? = null,
)

class NativePeer(private val runtime: MediaRuntime) {
    val identity = java.util.UUID.randomUUID().toString()
    val owner = MetadataOwner()
    private val closed = AtomicBoolean(false)
    private val failure = AtomicReference<PeerFailure?>(null)
    private val progress = AtomicReference(DecodedProgress())
    internal val renderWorkNs = java.util.concurrent.atomic.AtomicLong()
    internal val renderSwapNs = java.util.concurrent.atomic.AtomicLong()
    private val rendered = java.util.concurrent.atomic.AtomicLong()
    val renderedFrames
        get() = rendered.get()

    internal fun renderedFrame() {
        rendered.incrementAndGet()
    }

    private val executor = Executors.newSingleThreadExecutor { Thread(it, "camera-sdk") }
    private val sinks = CopyOnWriteArraySet<VideoSink>()
    private val renderers = CopyOnWriteArraySet<OwnedVideoSink>()
    private val ready = CompletableDeferred<Unit>()
    private val gathered = CompletableDeferred<Unit>()
    private val disposed = CompletableDeferred<Unit>()
    private var factory: PeerConnectionFactory? = null
    private var connection: PeerConnection? = null

    val frames: DecodedProgress
        get() = if (closed.get()) DecodedProgress() else progress.get()

    val failed: Boolean
        get() = failure.get() != null || owner.needsReplacement

    internal val failureCause
        get() = failure.get()

    val isClosed: Boolean
        get() = closed.get()

    fun retainRenderContext() = RenderContext(runtime.acquire(), runtime::release)

    private val counter = VideoSink { frame ->
        if (!closed.get()) {
            if (
                frame.rotatedWidth <= 0 ||
                    frame.rotatedHeight <= 0 ||
                    frame.rotatedWidth.toLong() * frame.rotatedHeight > 2073600
            ) {
                failure.compareAndSet(null, PeerFailure.FrameLimit)
            } else {
                val tag = frame.buffer as? TaggedBuffer
                val metadata = if (tag?.owner === owner) tag.annotation else null
                val before = progress.get()
                progress.set(
                    DecodedProgress(
                        before.count + 1,
                        frame.rotatedWidth,
                        frame.rotatedHeight,
                        SystemClock.elapsedRealtime(),
                        metadata,
                    )
                )
            }
        }
    }

    suspend fun offer(): String {
        ready.await()
        val created = CompletableDeferred<SessionDescription>()
        execute {
            connection!!.createOffer(
                object : ObserverAdapter() {
                    override fun onCreateSuccess(value: SessionDescription) {
                        created.complete(value)
                    }

                    override fun onCreateFailure(error: String) {
                        created.completeExceptionally(ClientException(Failure.Network))
                    }
                },
                MediaConstraints(),
            )
        }
        val description = created.await()
        val set = CompletableDeferred<Unit>()
        execute { connection!!.setLocalDescription(setObserver(set), description) }
        set.await()
        gathered.await()
        val result = CompletableDeferred<String>()
        execute { result.complete(connection!!.localDescription.description) }
        return result.await()
    }

    suspend fun answer(value: String) {
        checked(WhepClient.validSdp(value))
        val set = CompletableDeferred<Unit>()
        execute {
            connection!!.setRemoteDescription(
                setObserver(set),
                SessionDescription(SessionDescription.Type.ANSWER, value),
            )
        }
        set.await()
    }

    fun addSink(sink: VideoSink) {
        synchronized(sinks) {
            if (closed.get()) {
                if (sink is OwnedVideoSink) sink.close()
                return
            }
            if (sink is OwnedVideoSink) renderers.add(sink)
            sinks.add(sink)
        }
    }

    internal fun renderFailed() {
        failure.compareAndSet(null, PeerFailure.Render)
    }

    internal fun rendererRetired(sink: OwnedVideoSink) {
        renderers.remove(sink)
    }

    fun removeSink(sink: VideoSink) {
        sinks.remove(sink)
    }

    private fun execute(action: () -> Unit) {
        if (closed.get()) return
        try {
            executor.execute { if (!closed.get()) action() }
        } catch (_: java.util.concurrent.RejectedExecutionException) {}
    }

    fun close() {
        synchronized(sinks) { if (!closed.compareAndSet(false, true)) return }
        owner.close()
        progress.set(DecodedProgress())
        for (renderer in renderers) try {
            renderer.close()
        } catch (_: Exception) {
            owner.releaseFailed.set(true)
        }
        ready.completeExceptionally(ClientException(Failure.Unavailable))
        gathered.completeExceptionally(ClientException(Failure.Unavailable))
        executor.execute {
            try {
                sinks.clear()
                for (release in
                    listOf<() -> Unit>(
                        { connection?.close() },
                        { connection?.dispose() },
                        { factory?.dispose() },
                    )) {
                    try {
                        release()
                    } catch (_: Exception) {
                        owner.releaseFailed.set(true)
                    }
                }
                connection = null
                factory = null
            } finally {
                disposed.complete(Unit)
                executor.shutdown()
            }
        }
    }

    suspend fun closeAndJoin() =
        withContext(NonCancellable) {
            close()
            disposed.await()
            for (renderer in renderers) renderer.awaitClosed()
            renderers.clear()
            !owner.releaseFailed.get()
        }

    private fun setObserver(result: CompletableDeferred<Unit>) =
        object : ObserverAdapter() {
            override fun onSetSuccess() {
                result.complete(Unit)
            }

            override fun onSetFailure(error: String) {
                result.completeExceptionally(ClientException(Failure.Network))
            }
        }

    private open class ObserverAdapter : SdpObserver {
        override fun onCreateSuccess(value: SessionDescription) {}

        override fun onCreateFailure(error: String) {}

        override fun onSetSuccess() {}

        override fun onSetFailure(error: String) {}
    }

    private val observer =
        object : PeerConnection.Observer {
            override fun onIceGatheringChange(state: PeerConnection.IceGatheringState) {
                if (state == PeerConnection.IceGatheringState.COMPLETE) gathered.complete(Unit)
            }

            override fun onIceConnectionChange(state: PeerConnection.IceConnectionState) {
                if (!closed.get() && state == PeerConnection.IceConnectionState.FAILED)
                    failure.compareAndSet(null, PeerFailure.Connectivity)
            }

            override fun onConnectionChange(state: PeerConnection.PeerConnectionState) {
                if (!closed.get() && state == PeerConnection.PeerConnectionState.FAILED)
                    failure.compareAndSet(null, PeerFailure.Connectivity)
            }

            override fun onTrack(value: RtpTransceiver) {}

            override fun onSignalingChange(state: PeerConnection.SignalingState) {}

            override fun onIceConnectionReceivingChange(receiving: Boolean) {}

            override fun onIceCandidate(candidate: IceCandidate) {}

            override fun onIceCandidatesRemoved(candidates: Array<out IceCandidate>) {}

            override fun onAddStream(stream: MediaStream) {}

            override fun onRemoveStream(stream: MediaStream) {}

            override fun onDataChannel(channel: DataChannel) {
                channel.close()
            }

            override fun onRenegotiationNeeded() {}
        }

    init {
        executor.execute {
            try {
                factory =
                    PeerConnectionFactory.builder()
                        .setVideoDecoderFactory(
                            MetadataDecoderFactory(owner) { frame ->
                                if (!closed.get()) {
                                    counter.onFrame(frame)
                                    for (sink in sinks) try {
                                        sink.onFrame(frame)
                                    } catch (_: Exception) {
                                        failure.compareAndSet(null, PeerFailure.Sink)
                                    }
                                }
                            }
                        )
                        .createPeerConnectionFactory()
                val configuration =
                    PeerConnection.RTCConfiguration(emptyList()).apply {
                        sdpSemantics = PeerConnection.SdpSemantics.UNIFIED_PLAN
                        continualGatheringPolicy =
                            PeerConnection.ContinualGatheringPolicy.GATHER_ONCE
                    }
                connection =
                    factory!!.createPeerConnection(configuration, observer)
                        ?: throw ClientException(Failure.Unavailable)
                val receiver =
                    connection!!.addTransceiver(
                        MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO,
                        RtpTransceiver.RtpTransceiverInit(
                            RtpTransceiver.RtpTransceiverDirection.RECV_ONLY
                        ),
                    )
                val codecs =
                    factory!!
                        .getRtpReceiverCapabilities(MediaStreamTrack.MediaType.MEDIA_TYPE_VIDEO)
                        .codecs
                        .filter { it.name.equals("H264", true) }
                checked(codecs.isNotEmpty(), Failure.Unavailable)
                receiver.setCodecPreferences(codecs)
                ready.complete(Unit)
            } catch (_: Exception) {
                failure.compareAndSet(null, PeerFailure.Initialization)
                ready.completeExceptionally(ClientException(Failure.Unavailable))
            }
        }
    }
}
