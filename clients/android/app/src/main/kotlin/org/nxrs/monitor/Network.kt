package org.nxrs.monitor

import java.io.ByteArrayOutputStream
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.nxrs.monitor.core.*

/** The platform trust store and hostname verification remain authoritative. */
class Network(
    val client: OkHttpClient =
        OkHttpClient.Builder()
            .followRedirects(false)
            .followSslRedirects(false)
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .callTimeout(15, TimeUnit.SECONDS)
            .build()
) : HttpTransport {
    override suspend fun send(request: HttpRequest): HttpReply =
        withContext(Dispatchers.IO) {
            try {
                val body = request.body?.toRequestBody(request.contentType?.toMediaType())
                val native =
                    Request.Builder()
                        .url(request.url.toASCIIString())
                        .method(request.method, body)
                        .header("Authorization", request.credential.authorization())
                        .header("Cache-Control", "no-store")
                        .build()
                val call = client.newCall(native)
                call.timeout().timeout(request.timeoutMs.coerceIn(1, 15000), TimeUnit.MILLISECONDS)
                call.execute().use { response ->
                    try {
                        checked(
                            response.body?.contentLength()?.let { it <= 262144 } ?: true,
                            Failure.TooLarge,
                        )
                        val bytes = ByteArrayOutputStream()
                        response.body?.byteStream()?.use { input ->
                            val buffer = ByteArray(2048)
                            while (true) {
                                val count = input.read(buffer)
                                if (count < 0) break
                                checked(bytes.size() + count <= 262144, Failure.TooLarge)
                                bytes.write(buffer, 0, count)
                            }
                        }
                        HttpReply(
                            response.code,
                            response.header("Content-Type"),
                            response.header("Location"),
                            bytes.toByteArray(),
                        )
                    } catch (error: Exception) {
                        HttpReply(
                            response.code,
                            response.header("Content-Type"),
                            response.header("Location"),
                            byteArrayOf(),
                            (error as? ClientException)?.failure ?: Failure.Network,
                        )
                    }
                }
            } catch (error: ClientException) {
                throw error
            } catch (_: IOException) {
                throw ClientException(Failure.Network)
            }
        }
}
