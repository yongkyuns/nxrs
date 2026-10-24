package org.nxrs.monitor

import android.os.SystemClock
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.lifecycle.Lifecycle
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import java.io.File
import java.security.KeyStore
import java.security.cert.CertificateFactory
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager
import kotlinx.coroutines.*
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import okhttp3.OkHttpClient
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.nxrs.monitor.core.*
import org.nxrs.monitor.media.*

@RunWith(AndroidJUnit4::class)
class CameraQualificationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext

    @Serializable
    private class Fixture(
        val gateway: String,
        val mediaOrigin: String,
        val token: String,
        val rootCertificateBase64: String,
        val qualificationCase: String,
        val minimumFPS: Double,
    )

    @Serializable
    private data class Measurements(
        val case: String,
        val seconds: Double,
        @SerialName("decoded_fps") val decoded: List<Double>,
        @SerialName("rendered_fps") val rendered: List<Double>,
        @SerialName("codec_input_fps") val input: List<Double>,
        @SerialName("codec_output_fps") val output: List<Double>,
        @SerialName("process_cpu_ms") val cpu: Long,
        @SerialName("max_frames_per_20ms_sample") val burst: List<Long>,
        @SerialName("mean_draw_wall_ms") val draw: List<Double>,
        @SerialName("mean_swap_wall_ms") val swap: List<Double>,
        @SerialName("pixel_identity_mismatches") val mismatches: Int,
    )

    private fun onMain(action: () -> Unit) = instrumentation.runOnMainSync(action)

    private fun waitFor(label: String, timeout: Long = 25000, predicate: () -> Boolean) {
        val until = SystemClock.elapsedRealtime() + timeout
        while (SystemClock.elapsedRealtime() < until) {
            if (predicate()) return
            Thread.sleep(50)
        }
        fail(label)
    }

    @Test
    fun twoActualDecodersStayIndependentAndRetireBeforeReplacement() = runBlocking {
        val file = File(context.cacheDir, "qualification.json")
        org.junit.Assume.assumeTrue(
            "Requires the isolated clients/tests/multicamera_reference.py fixture",
            file.isFile,
        )
        val fixture = WireJson.decode<Fixture>(file.readBytes(), 32768)
        assertEquals("localhost", EndpointPolicy.parse(fixture.gateway).host)
        assertEquals("localhost", EndpointPolicy.parse(fixture.mediaOrigin).host)
        try {
            GatewayClient(EndpointPolicy(EndpointPolicy.parse(fixture.gateway)), Network())
                .cameras(Credential.parse(fixture.token))
            fail("Default transport trusted the uninstalled fixture root")
        } catch (error: ClientException) {
            assertEquals(Failure.Network, error.failure)
        }
        val certificate =
            CertificateFactory.getInstance("X.509")
                .generateCertificate(
                    android.util.Base64.decode(
                            fixture.rootCertificateBase64,
                            android.util.Base64.DEFAULT,
                        )
                        .inputStream()
                )
        val trust =
            TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
                .apply {
                    init(
                        KeyStore.getInstance(KeyStore.getDefaultType()).apply {
                            load(null)
                            setCertificateEntry("fixture", certificate)
                        }
                    )
                }
                .trustManagers
                .single() as X509TrustManager
        val tls = SSLContext.getInstance("TLS").apply { init(null, arrayOf(trust), null) }
        val network =
            Network(
                OkHttpClient.Builder()
                    .sslSocketFactory(tls.socketFactory, trust)
                    .followRedirects(false)
                    .followSslRedirects(false)
                    .readTimeout(15, java.util.concurrent.TimeUnit.SECONDS)
                    .build()
            )
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
        val store = EnrollmentStore(context, "qualification")
        val library = CaptureLibrary(context)
        lateinit var controller: HomeController
        val mismatches = AtomicInteger()
        val red = ConcurrentHashMap<CameraId, AtomicInteger>()
        val blue = ConcurrentHashMap<CameraId, AtomicInteger>()
        val epochs = ConcurrentHashMap<CameraId, MutableSet<String>>()
        val witnesses = ConcurrentHashMap<CameraId, RenderedSample>()
        val visibleCameras = java.util.concurrent.atomic.AtomicReference<Set<CameraId>>(emptySet())
        val pauseTrace = ArrayDeque<String>()
        var readPixels by mutableStateOf(true)
        onMain {
            controller = HomeController(store, network, MediaRuntime(context), library, scope)
            QualificationActivity.content = {
                val home by controller.state.collectAsState()
                Surface(Modifier.fillMaxSize()) {
                    CameraGrid(
                        home,
                        { ids ->
                            visibleCameras.set(ids)
                            controller.visible(ids)
                        },
                        controller::toggle,
                        controller::fullscreen,
                        witness =
                            if (readPixels)
                                { id, sample ->
                                    witnesses[id] = sample
                                    val metadata = sample.metadata
                                    if (metadata != null && sample.freshImage) {
                                        epochs
                                            .computeIfAbsent(id) { ConcurrentHashMap.newKeySet() }
                                            .add(metadata.epoch)
                                        when {
                                            sample.red > 150 && sample.blue < 100 -> {
                                                if (
                                                    metadata.presence() != Presence.Person ||
                                                        sample.boxesDrawn != 1
                                                )
                                                    mismatches.incrementAndGet()
                                                red.computeIfAbsent(id) { AtomicInteger() }
                                                    .incrementAndGet()
                                            }
                                            sample.blue > 150 && sample.red < 100 -> {
                                                if (
                                                    metadata.presence() != Presence.Clear ||
                                                        sample.boxesDrawn != 0
                                                )
                                                    mismatches.incrementAndGet()
                                                blue
                                                    .computeIfAbsent(id) { AtomicInteger() }
                                                    .incrementAndGet()
                                            }
                                            else -> mismatches.incrementAndGet()
                                        }
                                    }
                                }
                            else null,
                    )
                }
            }
        }
        val scenario = ActivityScenario.launch(QualificationActivity::class.java)
        val a = CameraId("cam_a")
        val b = CameraId("cam_b")
        fun clickCamera(action: String, id: CameraId) {
            val name = controller.state.value.sessions.getValue(id).camera.name
            val button =
                UiDevice.getInstance(instrumentation)
                    .wait(Until.findObject(By.desc("$action $name")), 5000)
            assertNotNull("$action control for $id is visible", button)
            button!!.click()
        }
        try {
            withContext(Dispatchers.Main) {
                controller.foreground(true)
                controller.enroll(fixture.gateway, fixture.mediaOrigin, fixture.token)
            }
            waitFor("catalog with three independently scoped cameras") {
                controller.state.value.cameras.size == 3
            }
            // Real layout visibility chooses the first two, not a design preview.
            waitFor("two simultaneously decoded and rendered streams") {
                listOf(a, b).all { (red[it]?.get() ?: 0) >= 15 && (blue[it]?.get() ?: 0) >= 15 }
            }
            assertEquals(0, mismatches.get())
            assertEquals(2, controller.state.value.sessions.values.count { it.occupiesSlot })
            val measuredPeers =
                listOf(a, b).map { controller.state.value.sessions.getValue(it).state.value.peer!! }
            // Synchronous GL pixel readback is an identity witness, not a
            // production operation. Keep the existing native surfaces and
            // measure their render path without diagnostic readback.
            onMain { readPixels = false }
            Thread.sleep(500)
            val rendered =
                listOf(a, b).map {
                    controller.state.value.sessions.getValue(it).state.value.peer!!.renderedFrames
                }
            val draw = measuredPeers.map { it.renderWorkNs.get() }
            val swap = measuredPeers.map { it.renderSwapNs.get() }
            val counts = measuredPeers.map { it.frames.count }
            val encoded = measuredPeers.map { it.owner.encoded.get() }
            val decoded = measuredPeers.map { it.owner.decoded.get() }
            val wall = SystemClock.elapsedRealtime()
            val cpu = android.os.Process.getElapsedCpuTime()
            val lastCount = counts.toMutableList()
            val burst = mutableListOf(0L, 0L)
            while (SystemClock.elapsedRealtime() - wall < 10000) {
                Thread.sleep(20)
                measuredPeers.forEachIndexed { i, p ->
                    val count = p.frames.count
                    burst[i] = maxOf(burst[i], count - lastCount[i])
                    lastCount[i] = count
                }
            }
            val elapsed = SystemClock.elapsedRealtime() - wall
            val fps =
                measuredPeers.mapIndexed { i, peer ->
                    (peer.frames.count - counts[i]) * 1000.0 / elapsed
                }
            val renderFps =
                measuredPeers.mapIndexed { i, p ->
                    (p.renderedFrames - rendered[i]) * 1000.0 / elapsed
                }
            val measured =
                Measurements(
                    fixture.qualificationCase,
                    elapsed / 1000.0,
                    fps,
                    renderFps,
                    measuredPeers.mapIndexed { i, p ->
                        (p.owner.encoded.get() - encoded[i]) * 1000.0 / elapsed
                    },
                    measuredPeers.mapIndexed { i, p ->
                        (p.owner.decoded.get() - decoded[i]) * 1000.0 / elapsed
                    },
                    android.os.Process.getElapsedCpuTime() - cpu,
                    burst,
                    measuredPeers.mapIndexed { i, p ->
                        (p.renderWorkNs.get() - draw[i]) /
                            1e6 /
                            (p.renderedFrames - rendered[i]).coerceAtLeast(1)
                    },
                    measuredPeers.mapIndexed { i, p ->
                        (p.renderSwapNs.get() - swap[i]) /
                            1e6 /
                            (p.renderedFrames - rendered[i]).coerceAtLeast(1)
                    },
                    mismatches.get(),
                )
            instrumentation.sendStatus(
                0,
                android.os.Bundle().apply {
                    putString(
                        "stream",
                        "\nMULTICAMERA METRICS: ${WireJson.json.encodeToString(measured)}\n",
                    )
                },
            )
            assertTrue(
                "${fixture.qualificationCase} case minimum decoded/rendered FPS: ${fixture.minimumFPS}; decoded=$fps rendered=$renderFps",
                (fps + renderFps).all { it >= fixture.minimumFPS },
            )
            onMain { readPixels = true }
            val pauseNeighbor = controller.state.value.sessions.getValue(b).state.value.peer!!
            val before = pauseNeighbor.frames.count
            clickCamera("Pause", a)
            waitFor("one camera pauses while the neighbor remains live") {
                val current = controller.state.value
                val trace =
                    "visible=${visibleCameras.get().sortedBy { it.value }} " +
                        current.sessions.entries.joinToString { (id, session) ->
                            val value = session.state.value
                            "$id:${value.status}:${value.peer?.identity?.take(8)}:${session.active}/${session.retiring}"
                        }
                if (pauseTrace.lastOrNull() != trace) {
                    if (pauseTrace.size == 32) pauseTrace.removeFirst()
                    pauseTrace.addLast(trace)
                }
                !controller.state.value.sessions.getValue(a).occupiesSlot &&
                    controller.state.value.sessions.getValue(b).state.value.peer ===
                        pauseNeighbor &&
                    controller.state.value.sessions
                        .getValue(b)
                        .state
                        .value
                        .peer
                        ?.frames
                        ?.count
                        ?.let { it > before + 10 } == true
            }
            val retained = controller.state.value.sessions.getValue(b).state.value.peer
            onMain { controller.fullscreen(b) }
            Thread.sleep(500)
            assertSame(retained, controller.state.value.sessions.getValue(b).state.value.peer)
            onMain { controller.fullscreen(null) }
            clickCamera("Watch", a)
            waitFor("camera resumes") {
                controller.state.value.sessions.getValue(a).state.value.live
            }
            // Request capture through the production session, not through test pixels.
            val initialSaved = library.list().size
            onMain {
                controller.state.value.sessions.getValue(a).snapshot()
                controller.state.value.sessions.getValue(b).toggleRecording()
            }
            waitFor("one-shot photo persisted") {
                runBlocking { library.list().size } > initialSaved
            }
            Thread.sleep(2500)
            onMain { controller.state.value.sessions.getValue(b).toggleRecording() }
            waitFor("bounded native video clip persisted") {
                runBlocking { library.list().any { it.video } }
            }
            val clips = library.list()
            assertTrue(clips.any { it.camera == a && !it.video })
            assertTrue(clips.any { it.camera == b && it.video })
            // Decode the saved clip with Android's native extractor, independently
            // of the encoder and the application's index.
            val clip = clips.last { it.video }
            val extractor = android.media.MediaExtractor()
            try {
                extractor.setDataSource(library.file(clip).absolutePath)
                assertEquals(1, extractor.trackCount)
                assertEquals(
                    "video/avc",
                    extractor.getTrackFormat(0).getString(android.media.MediaFormat.KEY_MIME),
                )
                extractor.selectTrack(0)
                assertTrue(extractor.sampleTime >= 0)
            } finally {
                extractor.release()
            }
            val neighbor = controller.state.value.sessions.getValue(b).state.value.peer!!
            val neighborCount = neighbor.frames.count
            checkStatus(
                network
                    .send(
                        HttpRequest(
                            EndpointPolicy.parse(fixture.gateway + "/_fixture/camera/cam_a/pause"),
                            "POST",
                            Credential.parse(fixture.token),
                            body = byteArrayOf(),
                        )
                    )
                    .status,
                204,
            )
            waitFor(
                "stalled camera detection becomes unavailable without stopping its neighbor",
                12000,
            ) {
                controller.state.value.sessions.getValue(a).state.value.presence ==
                    Presence.Unavailable && neighbor.frames.count > neighborCount + 25
            }
            assertSame(neighbor, controller.state.value.sessions.getValue(b).state.value.peer)
            checkStatus(
                network
                    .send(
                        HttpRequest(
                            EndpointPolicy.parse(fixture.gateway + "/_fixture/camera/cam_a/resume"),
                            "POST",
                            Credential.parse(fixture.token),
                            body = byteArrayOf(),
                        )
                    )
                    .status,
                204,
            )
            onMain { controller.retry(a) }
            waitFor("recovered source uses a new metadata epoch", 30000) {
                controller.state.value.sessions.getValue(a).state.value.live &&
                    (epochs[a]?.size ?: 0) >= 2
            }
            scenario.moveToState(Lifecycle.State.CREATED)
            onMain { controller.foreground(false) }
            waitFor("background releases both peers and annotation state") {
                controller.state.value.sessions.values.none { it.occupiesSlot }
            }
            assertTrue(
                controller.state.value.sessions.values.all {
                    it.state.value.peer == null && it.state.value.presence == Presence.Unavailable
                }
            )
            scenario.moveToState(Lifecycle.State.RESUMED)
            onMain { controller.foreground(true) }
            waitFor("foreground restarts both cameras") {
                controller.state.value.sessions.values.count { it.state.value.live } == 2
            }
            waitFor("foreground detection subscriptions remain valid") {
                listOf(a, b).all { controller.state.value.sessions.getValue(it).detectionAvailable }
            }
            // Two catalogs may reuse IDs; an enrollment replacement retires old
            // resources before slots can be reused by the newly bound sessions.
            val old = controller.state.value.sessions.values.toList()
            withContext(Dispatchers.Main) {
                controller.enroll(fixture.gateway, fixture.mediaOrigin, fixture.token)
            }
            waitFor("replaced enrollment has joined obsolete callbacks") {
                old.none { it.occupiesSlot }
            }
            waitFor("replacement catalog resumes") {
                controller.state.value.sessions.values.count { it.state.value.live } == 2
            }
            assertEquals(0, mismatches.get())
            instrumentation.sendStatus(
                0,
                android.os.Bundle().apply {
                    putString(
                        "stream",
                        "\nMULTICAMERA ANDROID PASS: two actual SDK decoders/renderers; independent pause; full-screen reuse; JPEG/MP4 capture; background and enrollment retirement; zero pixel/annotation mismatches\n",
                    )
                },
            )
        } finally {
            for (trace in pauseTrace) instrumentation.sendStatus(
                0,
                android.os.Bundle().apply {
                    putString("stream", "\nCAMERA DIAGNOSTIC: pause $trace\n")
                },
            )
            for ((id, session) in controller.state.value.sessions) {
                val value = session.state.value
                val frame = value.peer?.frames
                val sample = witnesses[id]
                instrumentation.sendStatus(
                    0,
                    android.os.Bundle().apply {
                        putString(
                            "stream",
                            "\nCAMERA DIAGNOSTIC: $id status=${value.status} frames=${frame?.count} mapped=${frame?.metadata != null} failed=${value.peer?.failed} detectionFailure=${session.detectionFailure} red=${red[id]?.get()} blue=${blue[id]?.get()} pixel=${sample?.red}/${sample?.green}/${sample?.blue} boxes=${sample?.boxesDrawn}\n",
                        )
                    },
                )
            }
            onMain { controller.close() }
            withContext(Dispatchers.Main) { controller.awaitClosed() }
            scenario.close()
            onMain { QualificationActivity.content = null }
            store.forget()
            file.delete()
            scope.cancel()
        }
    }

    @Test
    fun deviceLocalEnrollmentRejectsCorruptCiphertext() {
        val store = EnrollmentStore(context, "keystore-control")
        try {
            val home =
                Enrollment(
                    EndpointPolicy.parse("https://localhost"),
                    null,
                    Credential.parse("TEST_ONLY"),
                )
            store.save(home)
            assertEquals("https://localhost", store.read()!!.gateway.toString())
            val file = File(context.noBackupFilesDir, "keystore-control.enrollment")
            assertFalse(file.readBytes().toString(Charsets.ISO_8859_1).contains("TEST_ONLY"))
            val bytes = file.readBytes()
            bytes[bytes.lastIndex] = (bytes.last().toInt() xor 1).toByte()
            file.writeBytes(bytes)
            try {
                store.read()
                fail("corrupt authenticated ciphertext accepted")
            } catch (_: ClientException) {}
        } finally {
            store.forget()
        }
    }
}
