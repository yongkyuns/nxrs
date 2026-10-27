package org.nxrs.monitor.core

import java.net.URI
import kotlin.test.*
import kotlinx.coroutines.*
import kotlinx.coroutines.test.*
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.*
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ContractTest {
    private fun fixture(name: String) =
        javaClass.classLoader!!.getResourceAsStream(name)!!.use { it.readBytes() }

    private val policy =
        EndpointPolicy(
            URI("https://control.example.test"),
            listOf(URI("https://media.example.test")),
        )
    private val credential = Credential.parse("TEST_ONLY_NOT_A_CREDENTIAL")

    @Serializable
    private data class Case(
        val name: String,
        val budget: Int,
        val reserved: Int = 0,
        val requested: List<CameraId>,
        val active: Set<CameraId>,
        val retiring: Set<CameraId>,
        val blocked: Set<CameraId>,
        val start: List<CameraId>,
        val stop: List<CameraId>,
    )

    @Test
    fun sharedCameraAdmissionCases() {
        for (case in WireJson.decode<List<Case>>(fixture("camera-sessions.json"))) {
            val plan =
                ViewingPlan.make(
                    case.requested,
                    case.active,
                    case.retiring,
                    case.blocked,
                    case.reserved,
                    case.budget,
                )
            assertEquals(case.start, plan.start, case.name)
            assertEquals(case.stop, plan.stop, case.name)
        }
        assertFails { ViewingPlan.make(emptyList(), emptySet(), emptySet(), maximumActive = 0) }
        assertFails { ViewingPlan.make(List(65) { CameraId("cam_$it") }, emptySet(), emptySet()) }
    }

    @Test
    fun identifiersCredentialsAndTrustCannotBeExpanded() {
        for (id in listOf("", "a/b", "../a", "a b", "a".repeat(65), "café")) assertFails {
            CameraId(id)
        }
        for (token in listOf("", "a\r\nb", "a=b", "=", "a b")) assertFails {
            Credential.parse(token)
        }
        for (url in
            listOf(
                "http://host",
                "https://u:p@host",
                "https://host/a/../b",
                "https://host/%2e",
                "https://host?x=1",
                "https://host/#fragment",
            )) assertFails { EndpointPolicy.parse(url) }
        assertFails { policy.media(URI("https://evil.example.test/video")) }
        val endpoint = URI("https://media.example.test/camera/whep")
        assertEquals(
            URI("https://media.example.test/camera/session"),
            policy.session("session", endpoint),
        )
        assertFails { policy.session("//evil.example.test/session", endpoint) }
        assertFails { policy.session("https://media.example.test:444/session", endpoint) }
        assertEquals("<redacted credential>", credential.toString())
    }

    @Test
    fun sharedCatalogAndGrantContracts() = runTest {
        val requests = mutableListOf<HttpRequest>()
        val transport = HttpTransport { request ->
            requests += request
            if (request.method == "GET")
                HttpReply(200, "application/json", null, fixture("cameras.json"))
            else HttpReply(201, "application/json", null, fixture("grant.json"))
        }
        val api = GatewayClient(policy, transport) { 1700000000 }
        val cameras = api.cameras(credential)
        assertEquals(Source.Synthetic, cameras.single().source)
        val grant = api.grant(cameras.single().id, credential)
        assertEquals(cameras.single().id, grant.camera)
        assertEquals("POST", requests.last().method)
        assertFalse(grant.toString().contains("TEST_ONLY"))
        assertFalse(requests.first().toString().contains("example.test"))
        assertFails {
            GatewayClient(policy, transport) { 1700000060 }.grant(cameras.single().id, credential)
        }
        assertFails {
            GatewayClient(policy, transport) { 1699999000 }.grant(cameras.single().id, credential)
        }
        val duplicate =
            WireJson.json.parseToJsonElement(fixture("cameras.json").decodeToString()).jsonObject
        val entry = duplicate.getValue("cameras").jsonArray.single()
        val bytes =
            JsonObject(duplicate + ("cameras" to JsonArray(listOf(entry, entry))))
                .toString()
                .encodeToByteArray()
        assertFails {
            GatewayClient(policy, HttpTransport { HttpReply(200, "application/json", null, bytes) })
                .cameras(credential)
        }
    }

    @Test
    fun allocatedWhepSessionIsDeletedOnBadAnswer() = runTest {
        val methods = mutableListOf<String>()
        val transport = HttpTransport { request ->
            methods += request.method
            if (request.method == "DELETE") HttpReply(204, null, null, byteArrayOf())
            else HttpReply(201, "application/json", "session", "{}".encodeToByteArray())
        }
        val grant =
            PlaybackGrant(
                CameraId("cam_reference"),
                URI("https://media.example.test/reference/whep"),
                credential,
                100,
            )
        assertFails { WhepClient(policy, transport) { 0 }.open(grant, "v=0\r\n") }
        assertEquals(listOf("POST", "DELETE"), methods)
    }

    @Test
    fun startupDeadlineClosesLocallyBeforeJoiningAllocationDrain() = runTest {
        val response = CompletableDeferred<Unit>()
        val draining = CompletableDeferred<Unit>()
        var closes = 0
        var cleaned = false
        val operation = launch {
            try {
                withStartupBudget(timeoutMs = 100, abort = { closes++ }) {
                    withContext(NonCancellable) {
                        draining.complete(Unit)
                        response.await()
                        cleaned = true
                    }
                }
            } catch (_: TimeoutCancellationException) {}
        }
        draining.await()
        advanceTimeBy(101)
        runCurrent()
        assertEquals(1, closes)
        assertFalse(cleaned)
        assertTrue(operation.isActive)
        response.complete(Unit)
        operation.join()
        assertTrue(cleaned)
        assertEquals(1, closes)
    }

    @Test
    fun successfulStartupRetiresItsDeadlineWithoutClosingThePeer() = runTest {
        var closes = 0
        assertEquals(
            42,
            withStartupBudget(timeoutMs = 100, abort = { closes++ }) {
                delay(50)
                42
            },
        )
        advanceTimeBy(1000)
        runCurrent()
        assertEquals(0, closes)
    }

    @Test
    fun offlineWhepPublicationIsRetryableButMissingCatalogCameraIsNot() = runTest {
        val grant =
            PlaybackGrant(
                CameraId("cam_reference"),
                URI("https://media.example.test/reference/whep"),
                credential,
                100,
            )
        val transport = HttpTransport { HttpReply(404, null, null, byteArrayOf()) }
        val media =
            assertFailsWith<ClientException> {
                WhepClient(policy, transport) { 0 }.open(grant, "v=0\r\n")
            }
        assertEquals(Failure.Unavailable, media.failure)
        val catalog =
            assertFailsWith<ClientException> {
                GatewayClient(policy, transport).grant(grant.camera, credential)
            }
        assertEquals(Failure.NotFound, catalog.failure)
    }

    @Test
    fun allocatedResourceIsRetiredEvenWhenTheResponseBodyOverflowsOrDisconnects() = runTest {
        for (failure in listOf(Failure.TooLarge, Failure.Network)) {
            val methods = mutableListOf<String>()
            val transport = HttpTransport { request ->
                methods += request.method
                if (request.method == "DELETE") HttpReply(204, null, null, byteArrayOf())
                else HttpReply(201, "application/sdp", "session", byteArrayOf(), failure)
            }
            val grant =
                PlaybackGrant(
                    CameraId("cam_reference"),
                    URI("https://media.example.test/reference/whep"),
                    credential,
                    100,
                )
            assertFails { WhepClient(policy, transport) { 0 }.open(grant, "v=0\r\n") }
            assertEquals(listOf("POST", "DELETE"), methods)
        }
    }

    @Test
    fun cancellationDuringAllocationDrainsResponseAndDeletesResource() = runTest {
        val arrived = CompletableDeferred<Unit>()
        val reply = CompletableDeferred<Unit>()
        var deletes = 0
        val transport = HttpTransport { request ->
            if (request.method == "DELETE") {
                deletes++
                HttpReply(204, null, null, byteArrayOf())
            } else {
                arrived.complete(Unit)
                reply.await()
                HttpReply(201, "application/sdp", "session", "v=0\r\n".encodeToByteArray())
            }
        }
        val grant =
            PlaybackGrant(
                CameraId("cam_reference"),
                URI("https://media.example.test/reference/whep"),
                credential,
                100,
            )
        val job = launch { WhepClient(policy, transport) { 0 }.open(grant, "v=0\r\n") }
        arrived.await()
        job.cancel()
        reply.complete(Unit)
        job.join()
        assertTrue(job.isCancelled)
        assertEquals(1, deletes)
    }

    @Test
    fun redirectsAndWrongCameraGrantsAreRejected() = runTest {
        assertFails {
            GatewayClient(
                    policy,
                    HttpTransport { HttpReply(302, null, "https://evil.test", byteArrayOf()) },
                )
                .cameras(credential)
        }
        val api =
            GatewayClient(
                policy,
                HttpTransport { HttpReply(201, "application/json", null, fixture("grant.json")) },
            ) {
                1700000000
            }
        assertFails { api.grant(CameraId("other"), credential) }
    }

    @Test
    fun sharedVideoIdentityAndExpiry() {
        val json = WireJson.decode<VideoDetectionFrame>(fixture("video-detection.json"), 8192)
        json.validate()
        val parsed = VideoDetectionSei.decode(fixture("video-detection-sei.bin"))!!
        assertEquals(json, parsed)
        assertTrue(parsed.boxes.isNotEmpty())
        assertEquals(Presence.Unavailable, parsed.presence(5000))
        val aged = parsed.copy(captured = parsed.source!!.latest!!.captured + 1500)
        assertTrue(aged.boxes.isEmpty())
        assertFails { VideoDetectionSei.decode(ByteArray(1048577)) }
        assertFails {
            VideoDetectionSei.decode(
                fixture("video-detection-sei.bin") + fixture("video-detection-sei.bin")
            )
        }
        assertFails { VideoDetectionSei.decode(byteArrayOf(0, 0, 0, 100, 6)) }
    }

    @Test
    fun independentDetectionStatesExpireWithoutNetworkInput() {
        val value = WireJson.decode<DetectionSnapshot>(fixture("detection.json"), 8192)
        val a = DetectionState()
        val b = DetectionState()
        val start = 2000L
        val receipt = start + value.elapsed
        a.receive(value, value.camera, receipt, start)
        b.receive(value, value.camera, receipt, start)
        assertNotEquals(Presence.Unavailable, a.presence(receipt, true))
        a.reset()
        assertEquals(Presence.Unavailable, a.presence(receipt, true))
        assertNotEquals(Presence.Unavailable, b.presence(receipt, true))
        assertEquals(Presence.Unavailable, b.presence(receipt + 1500, true))
        assertFails { a.receive(value, CameraId("other"), receipt, start) }
        val delayed = DetectionState()
        delayed.receive(value, value.camera, receipt + 2000, start)
        assertEquals(Presence.Unavailable, delayed.presence(receipt + 2000, true))
    }

    @Test
    fun fragmentedSseIsBoundedAndDoesNotInventEvents() {
        val framer = DetectionFramer()
        val messages = mutableListOf<ByteArray>()
        val data =
            "event: detection\r\nid: abc:1\r\ndata: ".encodeToByteArray() +
                fixture("detection.json").decodeToString().replace("\n", "").encodeToByteArray() +
                "\r\n\r\n".encodeToByteArray()
        for (byte in data) framer.append(byteArrayOf(byte)) { _, payload -> messages += payload }
        assertEquals(1, messages.size)
        WireJson.decode<DetectionSnapshot>(messages.single(), 8192).validate(CameraId("cam_one"))
        assertFails { DetectionFramer().append(ByteArray(8193) { 65 }) { _, _ -> } }
        assertFails {
            DetectionFramer().append("event: detection\nevent: detection\n".encodeToByteArray()) {
                _,
                _ ->
            }
        }
    }

    @Test
    fun rotationAndLetterboxingFollowTheActualImage() {
        val b = Bounds(100, 200, 400, 600)
        val r = OverlayRectangle.fit(b, 640, 480, 0, 800f, 800f)!!
        assertEquals(80f, r.x)
        assertEquals(220f, r.y)
        assertEquals(240f, r.width)
        for ((rotation, expected) in
            listOf(90 to (340f to 80f), 180 to (480f to 340f), 270 to (220f to 480f))) {
            val rotated = OverlayRectangle.fit(b, 640, 480, rotation, 800f, 800f)!!
            assertEquals(expected.first, rotated.x, 0.001f)
            assertEquals(expected.second, rotated.y, 0.001f)
        }
        assertNull(OverlayRectangle.fit(b, 640, 480, 45, 800f, 800f))
        assertNull(OverlayRectangle.fit(b, 640, 480, 0, Float.NaN, 800f))
        assertNull(OverlayRectangle.fit(Bounds(-1, 0, 1000, 1000), 640, 480, 0, 800f, 800f))
    }
}
