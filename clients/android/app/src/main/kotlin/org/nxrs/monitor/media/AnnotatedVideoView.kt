package org.nxrs.monitor.media

import android.content.Context
import android.opengl.GLES20
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.view.SurfaceHolder
import android.view.SurfaceView
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import org.nxrs.monitor.core.*
import org.webrtc.*

data class RenderedSample(
    val red: Int,
    val green: Int,
    val blue: Int,
    val metadata: VideoDetectionFrame?,
    val boxesDrawn: Int,
    val freshImage: Boolean = true,
)

/**
 * One pending frame and one immutable I420 image already copied out of the codec. No idle display
 * pins a codec buffer. Pixels and boxes share one swap.
 */
@android.annotation.SuppressLint(
    "ViewConstructor"
) // An admitted peer is required; never allocate one for XML tools.
class AnnotatedVideoView(
    context: Context,
    private val peer: NativePeer,
    witness: ((RenderedSample) -> Unit)? = null,
) : SurfaceView(context), SurfaceHolder.Callback, OwnedVideoSink {
    @Volatile private var witness = witness

    /** Change diagnostic observation without replacing the camera's native surface. */
    fun observe(value: ((RenderedSample) -> Unit)?) {
        witness = value
    }

    private val thread = HandlerThread("camera-render").apply { start() }
    private val handler = Handler(thread.looper)
    private val closed = AtomicBoolean(false)
    private val finished = CompletableDeferred<Unit>()
    private val windowBarrier = CountDownLatch(1)
    private val lock = Any()
    private var pending: VideoFrame? = null
    private var queued = false
    private var lease: RenderContext? = null
    private var egl: EglBase? = null
    private var drawer: GlRectDrawer? = null
    private var frameDrawer: VideoFrameDrawer? = null
    private var image: VideoFrame? = null
    private var shader: GlShader? = null
    private var surface = false
    private var viewWidth = 0
    private var viewHeight = 0
    private var metadata: VideoDetectionFrame? = null
    private var renderedAt = 0L
    private var lastSample: RenderedSample? = null
    private val vertices =
        ByteBuffer.allocateDirect(32).order(ByteOrder.nativeOrder()).asFloatBuffer()
    val boxesDrawn = AtomicInteger(0)

    init {
        holder.addCallback(this)
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
                    if (
                        !closed.get() &&
                            surface &&
                            next.rotatedWidth > 0 &&
                            next.rotatedHeight > 0 &&
                            next.rotatedWidth.toLong() * next.rotatedHeight <= 2073600
                    )
                        render(next)
                } catch (_: Exception) {
                    peer.renderFailed()
                    close()
                } finally {
                    next.release()
                }
        }
    }

    override fun surfaceCreated(holder: SurfaceHolder) {
        handler.post {
            if (closed.get() || peer.isClosed) return@post
            try {
                if (egl == null) {
                    lease = peer.retainRenderContext()
                    egl = EglBase.create(lease!!.value, EglBase.CONFIG_PLAIN)
                }
                egl!!.createSurface(holder.surface)
                egl!!.makeCurrent()
                surface = true
                if (drawer == null) {
                    drawer = GlRectDrawer()
                    frameDrawer = VideoFrameDrawer()
                    shader =
                        GlShader(
                            "attribute vec2 position; void main(){ gl_Position=vec4(position,0.0,1.0); }",
                            "precision mediump float; void main(){ gl_FragColor=vec4(1.0,0.55,0.0,1.0); }",
                        )
                }
                handler.removeCallbacks(expiry)
                handler.post(expiry)
            } catch (_: Exception) {
                peer.renderFailed()
                close()
            }
        }
    }

    override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {
        handler.post {
            viewWidth = width
            viewHeight = height
            if (surface && !closed.get()) guardedDraw()
        }
    }

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        // Android may release the native window when this callback returns.
        if (closed.get()) {
            windowBarrier.await()
            return
        }
        val barrier = CountDownLatch(1)
        handler.post {
            try {
                surface = false
                egl?.detachCurrent()
                egl?.releaseSurface()
            } finally {
                barrier.countDown()
            }
        }
        barrier.await()
    }

    private fun render(frame: VideoFrame) {
        val owned = frame.buffer.toI420() ?: return
        image?.release()
        image = VideoFrame(owned, frame.rotation, frame.timestampNs)
        val tag = frame.buffer as? TaggedBuffer
        metadata = if (tag?.owner === peer.owner) tag.annotation else null
        renderedAt = SystemClock.elapsedRealtime()
        drawWindow(capturePixel = true)
        peer.renderedFrame()
    }

    private val expiry =
        object : Runnable {
            override fun run() {
                if (closed.get()) return
                if (
                    surface &&
                        boxesDrawn.get() > 0 &&
                        (metadata?.let { !peer.owner.accepts(it) } == true ||
                            SystemClock.elapsedRealtime() - renderedAt >=
                                (metadata?.remainingLifetimeMs ?: 0))
                ) {
                    guardedDraw()
                    lastSample?.let {
                        witness?.invoke(it.copy(boxesDrawn = boxesDrawn.get(), freshImage = false))
                    }
                }
                handler.postDelayed(this, 100)
            }
        }

    private fun guardedDraw() {
        try {
            drawWindow()
        } catch (_: Exception) {
            peer.renderFailed()
            close()
        }
    }

    private fun drawWindow(capturePixel: Boolean = false) {
        if (!surface || closed.get() || viewWidth <= 0 || viewHeight <= 0) return
        val started = SystemClock.elapsedRealtimeNanos()
        egl!!.makeCurrent()
        GLES20.glViewport(0, 0, viewWidth, viewHeight)
        GLES20.glClearColor(0f, 0f, 0f, 1f)
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT)
        var count = 0
        val frame = image
        // Use the same admission snapshot for the draw and its diagnostic sample.
        val annotation = metadata?.takeIf {
            peer.owner.accepts(it) &&
                (it.boxes.isEmpty() ||
                    SystemClock.elapsedRealtime() - renderedAt < it.remainingLifetimeMs)
        }
        if (frame != null) {
            val scale =
                minOf(
                    viewWidth.toFloat() / frame.rotatedWidth,
                    viewHeight.toFloat() / frame.rotatedHeight,
                )
            val width = (frame.rotatedWidth * scale).toInt()
            val height = (frame.rotatedHeight * scale).toInt()
            frameDrawer!!.drawFrame(
                frame,
                drawer!!,
                null,
                (viewWidth - width) / 2,
                (viewHeight - height) / 2,
                width,
                height,
            )
            if (annotation != null) {
                GLES20.glViewport(0, 0, viewWidth, viewHeight)
                val program = shader!!
                program.useProgram()
                val position = program.getAttribLocation("position")
                GLES20.glEnableVertexAttribArray(position)
                GLES20.glLineWidth(2f)
                for (obj in annotation.boxes) {
                    val b =
                        OverlayRectangle.fit(
                            obj.bounds,
                            frame.buffer.width,
                            frame.buffer.height,
                            frame.rotation,
                            viewWidth.toFloat(),
                            viewHeight.toFloat(),
                        ) ?: continue
                    val l = b.x * 2 / viewWidth - 1
                    val r = (b.x + b.width) * 2 / viewWidth - 1
                    val t = 1 - b.y * 2 / viewHeight
                    val d = 1 - (b.y + b.height) * 2 / viewHeight
                    vertices.clear()
                    vertices.put(l).put(t).put(r).put(t).put(r).put(d).put(l).put(d).position(0)
                    GLES20.glVertexAttribPointer(position, 2, GLES20.GL_FLOAT, false, 0, vertices)
                    GLES20.glDrawArrays(GLES20.GL_LINE_LOOP, 0, 4)
                    count++
                }
                GLES20.glDisableVertexAttribArray(position)
            }
        }
        boxesDrawn.set(count)
        val observer = witness
        val pixel =
            if (capturePixel && observer != null && frame != null)
                ByteBuffer.allocateDirect(4).also {
                    GLES20.glReadPixels(
                        viewWidth / 2,
                        viewHeight / 2,
                        1,
                        1,
                        GLES20.GL_RGBA,
                        GLES20.GL_UNSIGNED_BYTE,
                        it,
                    )
                }
            else null
        val submitted = SystemClock.elapsedRealtimeNanos()
        peer.renderWorkNs.addAndGet(submitted - started)
        egl!!.swapBuffers()
        peer.renderSwapNs.addAndGet(SystemClock.elapsedRealtimeNanos() - submitted)
        if (pixel != null) {
            val sample =
                RenderedSample(
                    pixel[0].toInt() and 255,
                    pixel[1].toInt() and 255,
                    pixel[2].toInt() and 255,
                    annotation,
                    count,
                )
            lastSample = sample
            observer?.invoke(sample)
        } else {
            lastSample = lastSample?.copy(metadata = annotation, boxesDrawn = count, freshImage = false)
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
                handler.removeCallbacks(expiry)
                if (egl != null) {
                    if (!egl!!.hasSurface()) egl!!.createDummyPbufferSurface()
                    egl!!.makeCurrent()
                    for (release in
                        listOf<() -> Unit>(
                            { shader?.release() },
                            { frameDrawer?.release() },
                            { drawer?.release() },
                        )) {
                        try {
                            release()
                        } catch (_: Exception) {
                            peer.renderFailed()
                        }
                    }
                }
            } catch (_: Exception) {
                peer.renderFailed()
            } finally {
                image?.release()
                image = null
                try {
                    egl?.release()
                } catch (_: Exception) {
                    peer.owner.releaseFailed.set(true)
                }
                egl = null
                metadata = null
                lastSample = null
                boxesDrawn.set(0)
                try {
                    lease?.close()
                } catch (_: Exception) {
                    peer.owner.releaseFailed.set(true)
                }
                lease = null
                finished.complete(Unit)
                windowBarrier.countDown()
                peer.rendererRetired(this)
                thread.quitSafely()
            }
        }
    }

    override suspend fun awaitClosed() {
        finished.await()
    }
}
