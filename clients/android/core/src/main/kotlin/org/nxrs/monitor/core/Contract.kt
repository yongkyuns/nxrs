package org.nxrs.monitor.core

import java.net.URI
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

enum class Failure {
    InvalidIdentifier,
    InvalidCredential,
    InvalidEndpoint,
    UntrustedOrigin,
    Contract,
    ExpiredGrant,
    Redirect,
    TooLarge,
    Unauthorized,
    Forbidden,
    NotFound,
    Unavailable,
    Busy,
    Network,
    TimedOut,
}

class ClientException(val failure: Failure) : Exception(failure.name)

fun checked(value: Boolean, failure: Failure = Failure.Contract) {
    if (!value) throw ClientException(failure)
}

@Serializable
@JvmInline
value class CameraId(val value: String) {
    init {
        checked(value.matches(Regex("[A-Za-z0-9_-]{1,64}")), Failure.InvalidIdentifier)
    }

    override fun toString() = value
}

/**
 * Secrets are neither serializable nor printable. Only transport and encrypted storage consume
 * them.
 */
class Credential private constructor(private val value: String) {
    fun authorization() = "Bearer $value"

    fun encodedForStorage() = value

    override fun toString() = "<redacted credential>"

    companion object {
        fun parse(value: String): Credential {
            checked(
                value.length in 1..8192 && value.matches(Regex("[A-Za-z0-9._~+/-]+=*")),
                Failure.InvalidCredential,
            )
            return Credential(value)
        }
    }
}

@Serializable
enum class Source {
    @SerialName("physical") Physical,
    @SerialName("synthetic") Synthetic,
}

@Serializable
enum class Availability {
    @SerialName("unknown") Unknown,
    @SerialName("online") Online,
    @SerialName("offline") Offline,
}

@Serializable
data class Camera(
    @SerialName("camera_id") val id: CameraId,
    @SerialName("gateway_id") val gateway: CameraId,
    @SerialName("site_id") val site: CameraId,
    val name: String,
    val source: Source,
    val availability: Availability,
    @SerialName("live_video") val liveVideo: Boolean,
)

@Serializable
internal class CameraList(
    @SerialName("api_version") val version: String,
    val cameras: List<Camera>,
)

@Serializable
internal class GrantResponse(
    @SerialName("api_version") val version: String,
    @SerialName("camera_id") val camera: CameraId,
    val protocol: String,
    val endpoint: String,
    @SerialName("access_token") val token: String,
    @SerialName("token_type") val tokenType: String,
    @SerialName("expires_at") val expiresAt: Long,
)

class PlaybackGrant(
    val camera: CameraId,
    val endpoint: URI,
    val credential: Credential,
    val expiresAt: Long,
) {
    override fun toString() = "<playback grant: redacted>"
}

object WireJson {
    val json = Json { ignoreUnknownKeys = true }

    inline fun <reified T> decode(bytes: ByteArray, maximum: Int = 262144): T {
        checked(bytes.size <= maximum, Failure.TooLarge)
        return try {
            json.decodeFromString<T>(bytes.decodeToString(throwOnInvalidSequence = true))
        } catch (_: Exception) {
            throw ClientException(Failure.Contract)
        }
    }
}

/** Enrollment is the complete origin allowlist. Responses cannot expand trust. */
class EndpointPolicy(gateway: URI, mediaOrigins: List<URI> = emptyList()) {
    val gateway = validate(gateway)
    private val origins =
        (mediaOrigins.map {
                val value = validate(it)
                checked(value.path.isNullOrEmpty() || value.path == "/", Failure.InvalidEndpoint)
                origin(value)
            } + origin(this.gateway))
            .toSet()

    fun media(uri: URI): URI {
        val value = validate(uri)
        checked(origin(value) in origins, Failure.UntrustedOrigin)
        return value
    }

    fun session(location: String, endpoint: URI): URI {
        media(endpoint)
        checked(
            location.isNotEmpty() && location.all { it.code in 33..126 && it != '\\' },
            Failure.InvalidEndpoint,
        )
        val resolved =
            try {
                validate(endpoint.resolve(location))
            } catch (_: Exception) {
                throw ClientException(Failure.InvalidEndpoint)
            }
        checked(origin(resolved) == origin(endpoint), Failure.UntrustedOrigin)
        return resolved
    }

    fun api(vararg path: String): URI =
        URI(gateway.toASCIIString().trimEnd('/') + "/" + path.joinToString("/"))

    companion object {
        fun parse(value: String): URI =
            try {
                validate(URI(value))
            } catch (_: Exception) {
                throw ClientException(Failure.InvalidEndpoint)
            }

        private fun validate(uri: URI): URI {
            checked(
                uri.scheme.equals("https", ignoreCase = true) &&
                    !uri.host.isNullOrEmpty() &&
                    uri.host.all { it.isLetterOrDigit() && it.code < 128 || it in "-.:[]" } &&
                    uri.rawUserInfo == null &&
                    uri.rawQuery == null &&
                    uri.rawFragment == null &&
                    (uri.port == -1 || uri.port in 1..65535) &&
                    !uri.rawPath.orEmpty().contains('%') &&
                    uri.rawPath.orEmpty().split('/').none { it == "." || it == ".." } &&
                    uri.toString().all { it.code in 33..126 && it != '\\' },
                Failure.InvalidEndpoint,
            )
            return uri
        }

        private fun origin(uri: URI) =
            uri.host.lowercase() to (if (uri.port == -1) 443 else uri.port)
    }
}

data class ViewingPlan(val start: List<CameraId>, val stop: List<CameraId>) {
    companion object {
        fun make(
            requested: List<CameraId>,
            active: Set<CameraId>,
            retiring: Set<CameraId>,
            blocked: Set<CameraId> = emptySet(),
            reservedSlots: Int = 0,
            maximumActive: Int = 2,
        ): ViewingPlan {
            checked(maximumActive in 1..4 && reservedSlots in 0..64 && requested.size <= 64)
            val wanted = requested.filterNot { it in blocked }.distinct().take(maximumActive)
            val occupied = active + retiring
            return ViewingPlan(
                wanted
                    .filterNot { it in occupied }
                    .take((maximumActive - occupied.size - reservedSlots).coerceAtLeast(0)),
                (active - wanted.toSet()).sortedBy { it.value },
            )
        }
    }
}
