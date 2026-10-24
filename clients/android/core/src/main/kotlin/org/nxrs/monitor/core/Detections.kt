package org.nxrs.monitor.core

import java.io.ByteArrayOutputStream
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
enum class Presence {
    @SerialName("unavailable") Unavailable,
    @SerialName("clear") Clear,
    @SerialName("person") Person,
}

@Serializable data class Bounds(val left: Int, val top: Int, val right: Int, val bottom: Int)

@Serializable
data class DetectedObject(
    @SerialName("class") val kind: String,
    @SerialName("score_permille") val score: Int,
    val bounds: Bounds,
)

@Serializable
data class Observation(
    @SerialName("frame_sequence") val sequence: Long,
    @SerialName("captured_ms") val captured: Long,
    @SerialName("inference_us") val inference: Long,
    @SerialName("person_score_permille") val score: Int,
    val objects: List<DetectedObject>,
    val truncated: Boolean,
)

@Serializable
data class DetectionEvent(val id: Long, @SerialName("at_ms") val at: Long, val kind: String)

@Serializable
data class DetectionSource(
    @SerialName("api_version") val version: String,
    @SerialName("publication_id") val publication: Long,
    @SerialName("emitted_ms") val emitted: Long,
    @SerialName("event_count") val eventCount: Long,
    val presence: Presence,
    val localization: Boolean,
    val latest: Observation? = null,
    val events: List<DetectionEvent>,
) {
    fun validate() {
        checked(
            version == "1" &&
                publication > 0 &&
                emitted >= 0 &&
                eventCount >= 0 &&
                events.size <= 8 &&
                (latest != null || presence == Presence.Unavailable)
        )
        var previous = 0L
        for (event in events) {
            checked(
                event.id > previous &&
                    event.id <= eventCount &&
                    event.at in 0..emitted &&
                    event.kind in setOf("person_entered", "person_left", "unavailable")
            )
            previous = event.id
        }
        checked(previous == eventCount)
        latest?.let { o ->
            checked(
                o.sequence > 0 &&
                    o.captured in 0..emitted &&
                    o.inference >= 0 &&
                    o.score in 0..1000 &&
                    o.objects.size <= 10 &&
                    (localization || (o.objects.isEmpty() && !o.truncated))
            )
            for (obj in o.objects) {
                val b = obj.bounds
                checked(
                    obj.kind == "person" &&
                        obj.score in 0..1000 &&
                        b.left >= 0 &&
                        b.left < b.right &&
                        b.right <= 1000 &&
                        b.top >= 0 &&
                        b.top < b.bottom &&
                        b.bottom <= 1000
                )
            }
        }
    }
}

@Serializable
data class DetectionSnapshot(
    @SerialName("api_version") val version: String,
    @SerialName("camera_id") val camera: CameraId,
    @SerialName("source_session_id") val session: String? = null,
    val revision: Long,
    @SerialName("stream_elapsed_ms") val elapsed: Long,
    @SerialName("source_online") val online: Boolean,
    val presence: Presence,
    @SerialName("observation_age_ms") val age: Long? = null,
    @SerialName("valid_for_ms") val validFor: Long,
    @SerialName("frame_synchronized") val synchronized: Boolean,
    val source: DetectionSource? = null,
    @SerialName("event_gap") val eventGap: Boolean,
) {
    fun validate(expected: CameraId) {
        checked(
            version == "1" &&
                camera == expected &&
                validFor in 1..1500 &&
                revision >= 0 &&
                elapsed >= 0 &&
                (age == null || age >= 0) &&
                (session?.matches(Regex("[0-9a-f]{32}")) ?: (source == null)) &&
                (source == null || session != null)
        )
        source?.validate()
        if (presence != Presence.Unavailable)
            checked(
                online &&
                    source?.presence == presence &&
                    source.latest != null &&
                    age != null &&
                    age in 0..4999
            )
    }
}

class DetectionState {
    var snapshot: DetectionSnapshot? = null
        private set

    var eventGap = false
        private set

    private var deadline = 0L
    private var receipt = 0L
    private var age = 0L

    fun receive(value: DetectionSnapshot, expected: CameraId, received: Long, started: Long) {
        value.validate(expected)
        checked(
            started >= 0 &&
                received >= started &&
                value.elapsed <=
                    (received - started).let {
                        if (it > Long.MAX_VALUE - 100) Long.MAX_VALUE else it + 100
                    }
        )
        val lag = (received - started - value.elapsed).coerceAtLeast(0)
        val prior = snapshot
        if (prior?.session == value.session && prior != null)
            checked(value.revision >= prior.revision)
        eventGap = value.eventGap
        if (prior?.session == value.session) {
            val count = prior?.source?.eventCount
            val first = value.source?.events?.firstOrNull()?.id
            eventGap =
                eventGap || (count != null && first != null && first > count && first - count > 1)
        }
        receipt = received
        deadline =
            received + minOf((value.validFor - lag).coerceAtLeast(0), Long.MAX_VALUE - received)
        age = minOf(value.age ?: 5000, 5000) + minOf(lag, 5000)
        snapshot = value
    }

    fun presence(now: Long, live: Boolean): Presence =
        if (live && now >= receipt && now < deadline && age < 5000 && now - receipt < 5000 - age)
            snapshot?.presence ?: Presence.Unavailable
        else Presence.Unavailable

    fun reset() {
        snapshot = null
        eventGap = false
        deadline = 0
        receipt = 0
        age = 0
    }
}

/** A subscription owns this framing state. Chunks, partial lines and payloads stay bounded. */
class DetectionFramer {
    private val line = ByteArrayOutputStream()
    private var event: String? = null
    private var id: String? = null
    private var payload: ByteArray? = null

    fun append(chunk: ByteArray, count: Int = chunk.size, emit: (String, ByteArray) -> Unit) {
        checked(count in 0..chunk.size)
        for (i in 0 until count) {
            val byte = chunk[i]
            if (byte != 10.toByte()) {
                checked(line.size() < 8192, Failure.TooLarge)
                line.write(byte.toInt())
                continue
            }
            var bytes = line.toByteArray()
            line.reset()
            if (bytes.lastOrNull() == 13.toByte()) bytes = bytes.copyOf(bytes.size - 1)
            if (bytes.isEmpty()) {
                payload?.let { value ->
                    val cursor = id
                    checked(
                        event == "detection" &&
                            cursor != null &&
                            cursor.length <= 100 &&
                            cursor.all { it.code in 33..126 }
                    )
                    emit(cursor!!, value)
                }
                event = null
                id = null
                payload = null
            } else if (bytes[0] != 58.toByte()) {
                val text =
                    try {
                        bytes.decodeToString(throwOnInvalidSequence = true)
                    } catch (_: Exception) {
                        throw ClientException(Failure.Contract)
                    }
                val colon = text.indexOf(':')
                checked(colon >= 0)
                val name = text.substring(0, colon)
                val value = text.substring(colon + 1).removePrefix(" ")
                when (name) {
                    "event" -> {
                        checked(event == null)
                        event = value
                    }
                    "id" -> {
                        checked(id == null)
                        id = value
                    }
                    "data" -> {
                        checked(payload == null)
                        payload = value.encodeToByteArray()
                    }
                    else -> throw ClientException(Failure.Contract)
                }
            }
        }
    }
}
