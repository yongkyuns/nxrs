package org.nxrs.monitor.media

import android.graphics.*
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaMuxer
import android.opengl.GLES20
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import org.nxrs.monitor.core.*
import org.webrtc.*

/**
 * A one-shot sink retains at most one image, returning its decoder buffer immediately after JPEG
 * conversion. No idle capture pins a codec texture.
 */
class NativeSnapshot(private val peer: NativePeer) : OwnedVideoSink {
    private val thread = HandlerThread("camera-photo").apply { start() }
    private val handler = Handler(thread.looper)
    private val closed = AtomicBoolean(false)
    private val accepted = AtomicBoolean(false)
    private val admission = Any()
    private val result = CompletableDeferred<ByteArray>()
    private val finished = CompletableDeferred<Unit>()

    init {
        peer.addSink(this)
    }

    override fun onFrame(frame: VideoFrame) =
        synchronized(admission) {
            if (closed.get() || !accepted.compareAndSet(false, true)) return@synchronized
            frame.retain()
            handler.post {
                try {
                    checked(
                        !closed.get() &&
                            frame.buffer.width > 0 &&
                            frame.buffer.height > 0 &&
                            frame.buffer.width.toLong() * frame.buffer.height <= 2073600,
                        Failure.Unavailable,
                    )
                    val data = frame.buffer.toI420() ?: throw ClientException(Failure.Unavailable)
                    try {
                        val w = data.width
                        val h = data.height
                        checked(w % 2 == 0 && h % 2 == 0)
                        val nv21 = ByteArray(w * h * 3 / 2)
                        val y = data.dataY.duplicate()
                        val u = data.dataU.duplicate()
                        val v = data.dataV.duplicate()
                        for (row in 0 until h) {
                            y.position(row * data.strideY)
                            y.get(nv21, row * w, w)
                        }
                        for (row in 0 until h / 2) for (col in 0 until w / 2) {
                            nv21[w * h + row * w + col * 2] = v[row * data.strideV + col]
                            nv21[w * h + row * w + col * 2 + 1] = u[row * data.strideU + col]
                        }
                        val output = ByteArrayOutputStream()
                        checked(
                            YuvImage(nv21, ImageFormat.NV21, w, h, null)
                                .compressToJpeg(Rect(0, 0, w, h), 90, output)
                        )
                        var bytes = output.toByteArray()
                        if (frame.rotation != 0) {
                            val bitmap =
                                BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
                                    ?: throw ClientException(Failure.Unavailable)
                            val rotated =
                                Bitmap.createBitmap(
                                    bitmap,
                                    0,
                                    0,
                                    w,
                                    h,
                                    Matrix().apply { postRotate(frame.rotation.toFloat()) },
                                    true,
                                )
                            try {
                                output.reset()
                                checked(rotated.compress(Bitmap.CompressFormat.JPEG, 90, output))
                                bytes = output.toByteArray()
                            } finally {
                                if (rotated !== bitmap) rotated.recycle()
                                bitmap.recycle()
                            }
                        }
                        if (!closed.get()) result.complete(bytes)
                    } finally {
                        data.release()
                    }
                } catch (_: Exception) {
                    result.completeExceptionally(ClientException(Failure.Unavailable))
                } finally {
                    frame.release()
                    close()
                }
            }
        }

    suspend fun jpeg(): ByteArray =
        try {
            result.await()
        } finally {
            close()
            withContext(NonCancellable) { awaitClosed() }
        }

    override fun close() =
        synchronized(admission) {
            if (!closed.compareAndSet(false, true)) return@synchronized
            peer.removeSink(this)
            result.completeExceptionally(ClientException(Failure.Unavailable))
            handler.post {
                finished.complete(Unit)
                peer.rendererRetired(this)
                thread.quitSafely()
            }
        }

    override suspend fun awaitClosed() {
        finished.await()
    }
}

data class ClipResult(val durationMs: Long)

/**
 * A bounded latest-frame sink, native surface encoder and muxer. Capturing never queues behind
 * storage: overload drops a frame; stop joins EOS/GL/codec.
 */
class NativeClipRecorder(private val peer: NativePeer, private val file: File) : OwnedVideoSink {
    private val thread = HandlerThread("camera-clip").apply { start() }
    private val handler = Handler(thread.looper)
    private val lease = peer.retainRenderContext()
    private val closed = AtomicBoolean(false)
    private val result = CompletableDeferred<ClipResult>()
    private val finished = CompletableDeferred<Unit>()
    private val lock = Any()
    private var pending: VideoFrame? = null
    private var queued = false
    private var codec: MediaCodec? = null
    private var muxer: MediaMuxer? = null
    private var input: android.view.Surface? = null
    private var egl: EglBase? = null
    private var drawer: GlRectDrawer? = null
    private var frameDrawer: VideoFrameDrawer? = null
    private var width = 0
    private var height = 0
    private var track = -1
    private var muxing = false
    private var began = 0L
    private var last = 0L
    private var outputFrames = 0
    private var bytes = 0L
    val isFinishing
        get() = closed.get()

    private var failed = false

    init {
        peer.addSink(this)
    }

    override fun onFrame(frame: VideoFrame) {
        synchronized(lock) {
            if (closed.get()) return
            frame.retain()
            pending?.release()
            pending = frame
            if (queued) return
            queued = true
        }
        handler.post {
            val next =
                synchronized(lock) {
                    queued = false
                    pending.also { pending = null }
                }
            if (next != null)
                try {
                    if (!closed.get()) encode(next)
                } catch (_: Exception) {
                    failed = true
                    close()
                } finally {
                    next.release()
                }
        }
    }

    private fun initialize(frame: VideoFrame) {
        width = frame.rotatedWidth
        height = frame.rotatedHeight
        checked(
            width > 0 &&
                height > 0 &&
                width % 2 == 0 &&
                height % 2 == 0 &&
                width.toLong() * height <= 2073600
        )
        val format =
            MediaFormat.createVideoFormat("video/avc", width, height).apply {
                setInteger(
                    MediaFormat.KEY_COLOR_FORMAT,
                    MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface,
                )
                setInteger(MediaFormat.KEY_BIT_RATE, 2_000_000)
                setInteger(MediaFormat.KEY_FRAME_RATE, 15)
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)
            }
        codec =
            MediaCodec.createEncoderByType("video/avc").apply {
                configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            }
        input = codec!!.createInputSurface()
        egl =
            EglBase.create(lease.value, EglBase.CONFIG_RECORDABLE).apply {
                createSurface(input)
                makeCurrent()
            }
        drawer = GlRectDrawer()
        frameDrawer = VideoFrameDrawer()
        muxer = MediaMuxer(file.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4)
        codec!!.start()
    }

    private fun encode(frame: VideoFrame) {
        val now = SystemClock.elapsedRealtimeNanos()
        if (last > 0 && now - last < 66_666_666) return
        if (began > 0 && now - began >= 60_000_000_000L) {
            close()
            return
        }
        if (codec == null) initialize(frame)
        checked(frame.rotatedWidth == width && frame.rotatedHeight == height)
        if (began == 0L) began = now
        egl!!.makeCurrent()
        GLES20.glViewport(0, 0, width, height)
        frameDrawer!!.drawFrame(frame, drawer!!, null, 0, 0, width, height)
        egl!!.swapBuffers(now - began)
        last = now
        drain(false)
        if (bytes >= 31L * 1024 * 1024) close()
    }

    private fun drain(ending: Boolean) {
        val native = codec ?: return
        val info = MediaCodec.BufferInfo()
        val deadline = SystemClock.elapsedRealtime() + if (ending) 5000 else 0
        while (true) {
            val index = native.dequeueOutputBuffer(info, if (ending) 10000 else 0)
            if (index == MediaCodec.INFO_TRY_AGAIN_LATER) {
                if (!ending) return
                checked(SystemClock.elapsedRealtime() < deadline, Failure.TimedOut)
                continue
            }
            if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                checked(!muxing)
                track = muxer!!.addTrack(native.outputFormat)
                muxer!!.start()
                muxing = true
            } else if (index >= 0) {
                try {
                    if (info.size > 0 && info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG == 0) {
                        checked(muxing && bytes + info.size <= 32L * 1024 * 1024, Failure.TooLarge)
                        val output =
                            native.getOutputBuffer(index)
                                ?: throw ClientException(Failure.Unavailable)
                        output.position(info.offset)
                        output.limit(info.offset + info.size)
                        muxer!!.writeSampleData(track, output, info)
                        bytes += info.size
                        outputFrames++
                    }
                } finally {
                    native.releaseOutputBuffer(index, false)
                }
                if (info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0) return
            }
            if (!ending && SystemClock.elapsedRealtime() > deadline + 10) return
        }
    }

    override fun close() {
        if (!closed.compareAndSet(false, true)) return
        peer.removeSink(this)
        synchronized(lock) {
            pending?.release()
            pending = null
        }
        handler.post {
            try {
                if (codec != null) {
                    codec!!.signalEndOfInputStream()
                    drain(true)
                }
                if (muxing) {
                    muxer!!.stop()
                    muxing = false
                }
                checked(!failed && outputFrames >= 2 && last > began, Failure.Unavailable)
                result.complete(ClipResult((last - began) / 1_000_000))
            } catch (_: Exception) {
                result.completeExceptionally(ClientException(Failure.Unavailable))
            } finally {
                // Attempt every release even if a vendor encoder rejects EOS.
                for (action in
                    listOf<() -> Unit>(
                        { frameDrawer?.release() },
                        { drawer?.release() },
                        { egl?.release() },
                        { input?.release() },
                        { codec?.stop() },
                        { codec?.release() },
                        { muxer?.release() },
                    )) {
                    try {
                        action()
                    } catch (_: Exception) {}
                }
                try {
                    lease.close()
                } catch (_: Exception) {
                    peer.owner.releaseFailed.set(true)
                }
                finished.complete(Unit)
                peer.rendererRetired(this)
                thread.quitSafely()
            }
        }
    }

    suspend fun finish(): ClipResult =
        withContext(NonCancellable) {
            close()
            awaitClosed()
            result.await()
        }

    override suspend fun awaitClosed() {
        finished.await()
    }
}
