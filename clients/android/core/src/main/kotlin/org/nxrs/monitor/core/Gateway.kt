package org.nxrs.monitor.core

import java.net.URI
import kotlinx.coroutines.Job
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext

class HttpRequest(
    val url: URI,
    val method: String,
    val credential: Credential,
    val contentType: String? = null,
    val body: ByteArray? = null,
    val timeoutMs: Long = 15000,
) {
    override fun toString() = "<HTTP request: redacted>"
}

class HttpReply(
    val status: Int,
    val contentType: String?,
    val location: String?,
    val body: ByteArray,
    val bodyFailure: Failure? = null,
) {
    override fun toString() = "<HTTP reply: redacted>"
}

fun interface HttpTransport {
    suspend fun send(request: HttpRequest): HttpReply
}

fun checkStatus(status: Int, expected: Int) {
    if (status == expected) return
    throw ClientException(
        when (status) {
            in 300..399 -> Failure.Redirect
            401 -> Failure.Unauthorized
            403 -> Failure.Forbidden
            404 -> Failure.NotFound
            409 -> Failure.Unavailable
            429 -> Failure.Busy
            else -> Failure.Network
        }
    )
}

fun mime(value: String?) = value?.substringBefore(';')?.trim()?.lowercase()

class GatewayClient(
    val policy: EndpointPolicy,
    private val transport: HttpTransport,
    private val nowSeconds: () -> Long = { System.currentTimeMillis() / 1000 },
) {
    suspend fun cameras(credential: Credential): List<Camera> {
        val r = transport.send(HttpRequest(policy.api("v1", "cameras"), "GET", credential))
        checkStatus(r.status, 200)
        r.bodyFailure?.let { throw ClientException(it) }
        checked(mime(r.contentType) == "application/json")
        val value = WireJson.decode<CameraList>(r.body)
        checked(
            value.version == "1" &&
                value.cameras.size <= 64 &&
                value.cameras.map { it.id }.distinct().size == value.cameras.size &&
                value.cameras.all {
                    it.name.isNotEmpty() && it.name.codePointCount(0, it.name.length) <= 128
                }
        )
        return value.cameras
    }

    suspend fun grant(
        camera: CameraId,
        credential: Credential,
        timeoutMs: Long = 15000,
    ): PlaybackGrant {
        val r =
            transport.send(
                HttpRequest(
                    policy.api("v1", "cameras", camera.value, "playback-grants"),
                    "POST",
                    credential,
                    "application/json",
                    "{}".encodeToByteArray(),
                    timeoutMs,
                )
            )
        checkStatus(r.status, 201)
        r.bodyFailure?.let { throw ClientException(it) }
        checked(mime(r.contentType) == "application/json")
        val value = WireJson.decode<GrantResponse>(r.body)
        checked(
            value.version == "1" &&
                value.protocol == "whep" &&
                value.tokenType == "Bearer" &&
                value.camera == camera
        )
        val now = nowSeconds()
        checked(value.expiresAt > now, Failure.ExpiredGrant)
        checked(value.expiresAt - now <= 330)
        return PlaybackGrant(
            camera,
            policy.media(EndpointPolicy.parse(value.endpoint)),
            Credential.parse(value.token),
            value.expiresAt,
        )
    }
}

class WhepSession
internal constructor(
    internal val url: URI,
    internal val credential: Credential,
    val answer: String,
) {
    override fun toString() = "<WHEP session: redacted>"
}

class WhepClient(
    private val policy: EndpointPolicy,
    private val transport: HttpTransport,
    private val nowSeconds: () -> Long = { System.currentTimeMillis() / 1000 },
) {
    suspend fun open(grant: PlaybackGrant, offer: String, timeoutMs: Long = 15000): WhepSession {
        checked(grant.expiresAt > nowSeconds(), Failure.ExpiredGrant)
        checked(validSdp(offer))
        policy.media(grant.endpoint)
        val caller = currentCoroutineContext()[Job]
        // A successful POST owns a server resource. Drain its bounded response
        // even when cancelled so Location is captured before remote cleanup.
        return withContext(NonCancellable) {
            val r =
                transport.send(
                    HttpRequest(
                        grant.endpoint,
                        "POST",
                        grant.credential,
                        "application/sdp",
                        offer.encodeToByteArray(),
                        timeoutMs,
                    )
                )
            if (r.status in setOf(404, 409, 410)) throw ClientException(Failure.Unavailable)
            checkStatus(r.status, 201)
            val location = r.location ?: throw ClientException(Failure.Contract)
            val url = policy.session(location, grant.endpoint)
            val provisional = WhepSession(url, grant.credential, "")
            try {
                r.bodyFailure?.let { throw ClientException(it) }
                caller?.ensureActive()
                checked(mime(r.contentType) == "application/sdp" && r.body.size <= 262144)
                val answer = r.body.decodeToString(throwOnInvalidSequence = true)
                checked(validSdp(answer))
                WhepSession(url, grant.credential, answer)
            } catch (error: Exception) {
                close(provisional)
                throw error
            }
        }
    }

    suspend fun close(session: WhepSession): Boolean =
        withContext(NonCancellable) {
            try {
                transport
                    .send(HttpRequest(session.url, "DELETE", session.credential, timeoutMs = 5000))
                    .status in setOf(200, 204, 404, 410)
            } catch (_: Exception) {
                false
            }
        }

    companion object {
        fun validSdp(value: String) =
            value.startsWith("v=0\r\n") &&
                value.encodeToByteArray().size <= 262144 &&
                !value.contains('\u0000')
    }
}
