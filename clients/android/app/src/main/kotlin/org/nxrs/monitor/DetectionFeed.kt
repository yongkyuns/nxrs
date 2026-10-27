package org.nxrs.monitor

import android.os.SystemClock
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.buffer
import kotlinx.coroutines.flow.callbackFlow
import okhttp3.*
import org.nxrs.monitor.core.*

data class DetectionMessage(val snapshot: DetectionSnapshot, val received: Long, val started: Long)

/**
 * One cancellable socket and a single latest event per camera. The parser has fixed bounds;
 * consumers cannot hold the SDK or capture queues behind SSE.
 */
class DetectionFeed(private val network: Network) {
    fun subscribe(home: Enrollment, camera: CameraId, cursor: String?, remember: (String) -> Unit) =
        callbackFlow {
                val started = SystemClock.elapsedRealtime()
                val request =
                    Request.Builder()
                        .url(
                            home.policy
                                .api("v1", "cameras", camera.value, "detections")
                                .toASCIIString()
                        )
                        .header("Authorization", home.credential.authorization())
                        .header("Accept", "text/event-stream")
                        .header("Cache-Control", "no-store")
                        .apply { if (cursor != null) header("Last-Event-ID", cursor) }
                        .build()
                val call = network.client.newCall(request)
                call.timeout().timeout(300, TimeUnit.SECONDS)
                call.enqueue(
                    object : Callback {
                        override fun onFailure(call: Call, error: IOException) {
                            close(ClientException(Failure.Network))
                        }

                        override fun onResponse(call: Call, response: Response) {
                            try {
                                response.use { r ->
                                    checkStatus(r.code, 200)
                                    checked(mime(r.header("Content-Type")) == "text/event-stream")
                                    val framer = DetectionFramer()
                                    var elapsed = 0L
                                    val input =
                                        r.body?.byteStream()
                                            ?: throw ClientException(Failure.Contract)
                                    val chunk = ByteArray(2048)
                                    while (!call.isCanceled()) {
                                        val n = input.read(chunk)
                                        if (n < 0) break
                                        framer.append(chunk, n) { id, bytes ->
                                            val value =
                                                WireJson.decode<DetectionSnapshot>(bytes, 8192)
                                            value.validate(camera)
                                            checked(
                                                value.elapsed >= elapsed &&
                                                    id ==
                                                        "${value.session ?: "none"}:${value.source?.eventCount ?: 0}"
                                            )
                                            elapsed = value.elapsed
                                            remember(id)
                                            trySend(
                                                DetectionMessage(
                                                    value,
                                                    SystemClock.elapsedRealtime(),
                                                    started,
                                                )
                                            )
                                        }
                                    }
                                }
                                close()
                            } catch (error: Exception) {
                                close(
                                    if (error is ClientException) error
                                    else ClientException(Failure.Network)
                                )
                            }
                        }
                    }
                )
                awaitClose { call.cancel() }
            }
            .buffer(Channel.CONFLATED)
}
