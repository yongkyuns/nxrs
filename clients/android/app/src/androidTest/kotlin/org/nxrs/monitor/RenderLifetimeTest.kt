package org.nxrs.monitor

import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.nxrs.monitor.core.*
import org.nxrs.monitor.media.*
import org.webrtc.JavaI420Buffer
import org.webrtc.VideoFrame

@RunWith(AndroidJUnit4::class)
class RenderLifetimeTest {
    private fun until(label: String, timeout: Long = 5000, predicate: () -> Boolean) {
        val end = SystemClock.elapsedRealtime() + timeout
        while (SystemClock.elapsedRealtime() < end) {
            if (predicate()) return
            Thread.sleep(20)
        }
        fail(label)
    }

    @Test
    fun frozenImageExpiresWithoutNewInputAndOwnerClosureFencesLateFrames() = runBlocking {
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val peer = NativePeer(MediaRuntime(instrumentation.targetContext))
        val samples = AtomicInteger()
        val latest = AtomicReference<RenderedSample?>(null)
        var view: AnnotatedVideoView? = null
        val scenario = ActivityScenario.launch(QualificationActivity::class.java)
        val source =
            DetectionSource(
                "1",
                1,
                1000,
                0,
                Presence.Person,
                true,
                Observation(
                    1,
                    1000,
                    0,
                    900,
                    listOf(DetectedObject("person", 900, Bounds(100, 100, 900, 900))),
                    false,
                ),
                emptyList(),
            )
        val metadata = VideoDetectionFrame("1", "1".repeat(32), 1, 1000, source)
        peer.owner.selectEpoch(metadata.epoch)
        val buffer = JavaI420Buffer.allocate(320, 240)
        for ((plane, value) in
            listOf(buffer.dataY to 76, buffer.dataU to 85, buffer.dataV to 255)) {
            while (plane.hasRemaining()) plane.put(value.toByte())
            plane.rewind()
        }
        val frame = VideoFrame(tagged(buffer, peer.owner, metadata), 0, 1000000)
        try {
            scenario.onActivity { activity ->
                view =
                    AnnotatedVideoView(activity, peer) { sample ->
                        latest.set(sample)
                        samples.incrementAndGet()
                    }
                activity.setContentView(view)
            }
            val end = SystemClock.elapsedRealtime() + 5000
            while (latest.get()?.boxesDrawn != 1 && SystemClock.elapsedRealtime() < end) {
                view!!.onFrame(frame)
                Thread.sleep(50)
            }
            assertEquals(1, latest.get()?.boxesDrawn)
            until("Boxes must expire on the frozen actual rendered image", 2500) {
                latest.get()?.let { !it.freshImage && it.boxesDrawn == 0 } == true
            }
            assertNull("Expired annotation must not be reported as admitted", latest.get()?.metadata)
            val expired = samples.get()
            Thread.sleep(350)
            assertEquals("Idle expiry must stop submitting GPU redraws", expired, samples.get())
            view!!.onFrame(frame)
            until("Freshly rendered frame is annotated") {
                latest.get()?.freshImage == true && latest.get()?.boxesDrawn == 1
            }
            peer.owner.selectEpoch("2".repeat(32))
            until("An obsolete source epoch must clear its frozen overlay", 500) {
                latest.get()?.boxesDrawn == 0
            }
            assertNull("Retired source metadata must not remain admitted", latest.get()?.metadata)
            buffer.retain()
            val replacement =
                VideoFrame(
                    tagged(buffer, peer.owner, metadata.copy(epoch = "2".repeat(32))),
                    0,
                    2000000,
                )
            try {
                view!!.onFrame(replacement)
                until("Replacement source owns the annotation") { latest.get()?.boxesDrawn == 1 }
                peer.owner.invalidate(MappingFailure.MissingOutput)
                until("Ambiguous decoder output clears a frozen overlay without new input", 500) {
                    latest.get()?.boxesDrawn == 0
                }
                assertNull("Ambiguous metadata must not remain admitted", latest.get()?.metadata)
            } finally {
                replacement.release()
            }
            peer.closeAndJoin()
            val count = samples.get()
            view!!.onFrame(frame)
            Thread.sleep(100)
            assertEquals(count, samples.get())
            assertEquals(0, view!!.boxesDrawn.get())
            assertFalse(peer.owner.qualified)
            assertEquals(MappingFailure.MissingOutput, peer.owner.mappingFailure)
        } finally {
            frame.release()
            peer.closeAndJoin()
            scenario.close()
        }
    }
}
