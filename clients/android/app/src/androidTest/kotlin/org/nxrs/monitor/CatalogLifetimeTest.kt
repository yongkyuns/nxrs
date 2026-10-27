package org.nxrs.monitor

import androidx.test.platform.app.InstrumentationRegistry
import java.util.UUID
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.*
import org.junit.Test
import org.nxrs.monitor.core.CameraId
import org.nxrs.monitor.media.MediaRuntime

class CatalogLifetimeTest {
    private fun catalog(vararg ids: String): String =
        """{"api_version":"1","cameras":[${ids.joinToString(",") {
            """{"camera_id":"$it","gateway_id":"gateway","site_id":"site","name":"$it","source":"synthetic","availability":"unknown","live_video":false}"""
        }}]}"""

    @Test
    fun removedFullscreenCameraReturnsToRemainingCatalog() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val response = AtomicReference(catalog("cam_a", "cam_b"))
        val network =
            Network(
                OkHttpClient.Builder()
                    .addInterceptor { chain ->
                        check(chain.request().url.encodedPath == "/v1/cameras")
                        Response.Builder()
                            .request(chain.request())
                            .protocol(Protocol.HTTP_1_1)
                            .code(200)
                            .message("OK")
                            .header("Content-Type", "application/json")
                            .body(response.get().toResponseBody("application/json".toMediaType()))
                            .build()
                    }
                    .build()
            )
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
        val store = EnrollmentStore(context, "catalog-test-" + UUID.randomUUID())
        val controller =
            HomeController(store, network, MediaRuntime(context), CaptureLibrary(context), scope)
        val a = CameraId("cam_a")
        val b = CameraId("cam_b")
        suspend fun loaded(ids: List<CameraId>) {
            withTimeout(5000) {
                while (
                    controller.state.value.loading ||
                        controller.state.value.cameras.map { it.id } != ids
                ) {
                    assertNull("Catalog request failed", controller.state.value.error)
                    delay(10)
                }
            }
        }
        try {
            withContext(Dispatchers.Main) {
                controller.foreground(true)
                controller.enroll("https://catalog.invalid", "", "catalog-fixture-key")
                assertNull("Fixture enrollment failed", controller.state.value.error)
                loaded(listOf(a, b))
                val original = controller.state.value.sessions.getValue(a)
                controller.fullscreen(b)
                assertEquals(b, controller.state.value.fullscreen)
                response.set(catalog("cam_a"))
                controller.refresh()
                loaded(listOf(a))
                assertNull(controller.state.value.fullscreen)
                assertEquals(a, controller.state.value.selected)
                assertSame(original, controller.state.value.sessions[a])
                response.set(catalog("cam_a", "cam_c"))
                controller.refresh()
                loaded(listOf(a, CameraId("cam_c")))
                assertSame(original, controller.state.value.sessions[a])
                assertEquals(a, controller.state.value.selected)
                controller.fullscreen(a)
                response.set(catalog())
                controller.refresh()
                loaded(emptyList())
                assertNull(controller.state.value.fullscreen)
                assertNull(controller.state.value.selected)
                assertTrue(controller.state.value.sessions.isEmpty())
            }
        } finally {
            withContext(NonCancellable + Dispatchers.Main) {
                controller.close()
                controller.awaitClosed()
                controller.forget()
            }
            scope.cancel()
        }
    }
}
