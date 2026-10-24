package org.nxrs.monitor.core

import java.io.ByteArrayOutputStream
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

@Serializable
data class VideoDetectionFrame(
    @SerialName("api_version") val version: String,
    val epoch: String,
    @SerialName("frame_sequence") val sequence: Long,
    @SerialName("frame_capture_ms") val captured: Long,
    val source: DetectionSource? = null,
) {
    fun validate() {
        checked(
            version == "1" && sequence > 0 && captured >= 0 && epoch.matches(Regex("[0-9a-f]{32}"))
        )
        source?.let { s ->
            s.validate()
            checked(s.emitted <= captured || s.emitted - captured <= 1000)
            s.latest?.let { checked(it.sequence <= sequence && it.captured <= captured) }
        }
    }

    fun presence(additionalAgeMs: Long = 0): Presence {
        val s = source ?: return Presence.Unavailable
        val o = s.latest ?: return Presence.Unavailable
        val delta = captured - o.captured
        return if (additionalAgeMs >= 0 && delta in 0..4999 && additionalAgeMs < 5000 - delta)
            s.presence
        else Presence.Unavailable
    }

    val boxes: List<DetectedObject>
        get() =
            source?.let { s ->
                val o = s.latest
                if (
                    s.localization &&
                        s.presence == Presence.Person &&
                        o != null &&
                        captured - o.captured in 0..1499
                )
                    o.objects
                else emptyList()
            } ?: emptyList()

    val remainingLifetimeMs: Long
        get() = if (boxes.isNotEmpty()) 1500 - (captured - source!!.latest!!.captured) else 0
}

object VideoDetectionSei {
    private val uuid = "nxrs-video-v1".encodeToByteArray() + byteArrayOf(0, 0, 0)

    fun decode(bytes: ByteArray): VideoDetectionFrame? {
        checked(bytes.size <= 1048576, Failure.TooLarge)
        var result: VideoDetectionFrame? = null
        fun nal(start: Int, end: Int) {
            if (start >= end || (bytes[start].toInt() and 31) != 6) return
            val out = ByteArrayOutputStream()
            var zeros = 0
            for (i in start + 1 until end) {
                val v = bytes[i].toInt() and 255
                if (zeros >= 2 && v == 3) {
                    zeros = 0
                    continue
                }
                checked(out.size() < 8192, Failure.TooLarge)
                out.write(v)
                zeros = if (v == 0) zeros + 1 else 0
            }
            val rbsp = out.toByteArray()
            var at = 0
            fun number(): Int {
                var value = 0
                while (at < rbsp.size) {
                    val v = rbsp[at++].toInt() and 255
                    value += v
                    checked(value <= 8192, Failure.TooLarge)
                    if (v != 255) return value
                }
                throw ClientException(Failure.Contract)
            }
            while (at < rbsp.size && (rbsp[at].toInt() and 255) != 128) {
                val kind = number()
                val length = number()
                checked(length <= rbsp.size - at)
                if (
                    kind == 5 &&
                        length >= uuid.size &&
                        uuid.indices.all { rbsp[at + it] == uuid[it] }
                ) {
                    checked(result == null)
                    val value =
                        WireJson.decode<VideoDetectionFrame>(
                            rbsp.copyOfRange(at + uuid.size, at + length),
                            8192,
                        )
                    value.validate()
                    result = value
                }
                at += length
            }
        }
        fun prefix(at: Int): Int {
            if (at + 3 <= bytes.size && bytes[at] == 0.toByte() && bytes[at + 1] == 0.toByte()) {
                if (bytes[at + 2] == 1.toByte()) return 3
                if (
                    at + 4 <= bytes.size &&
                        bytes[at + 2] == 0.toByte() &&
                        bytes[at + 3] == 1.toByte()
                )
                    return 4
            }
            return 0
        }
        if (prefix(0) > 0) {
            var at = 0
            while (at < bytes.size) {
                val p = prefix(at)
                checked(p > 0)
                val start = at + p
                var end = start
                while (end < bytes.size && prefix(end) == 0) end++
                nal(start, end)
                at = end
            }
        } else {
            var at = 0
            while (at < bytes.size) {
                checked(bytes.size - at >= 4)
                val length =
                    (0..3).fold(0L) { value, i ->
                        (value shl 8) or (bytes[at + i].toLong() and 255)
                    }
                at += 4
                checked(length > 0 && length <= bytes.size - at)
                nal(at, at + length.toInt())
                at += length.toInt()
            }
        }
        return result
    }
}

data class OverlayRectangle(val x: Float, val y: Float, val width: Float, val height: Float) {
    companion object {
        fun fit(
            bounds: Bounds,
            width: Int,
            height: Int,
            rotation: Int,
            viewWidth: Float,
            viewHeight: Float,
        ): OverlayRectangle? {
            if (
                width <= 0 ||
                    height <= 0 ||
                    viewWidth <= 0 ||
                    viewHeight <= 0 ||
                    rotation !in setOf(0, 90, 180, 270)
            )
                return null
            val left = bounds.left / 1000f
            val top = bounds.top / 1000f
            val right = bounds.right / 1000f
            val bottom = bounds.bottom / 1000f
            if (
                !viewWidth.isFinite() ||
                    !viewHeight.isFinite() ||
                    bounds.left !in 0..999 ||
                    bounds.right !in 1..1000 ||
                    bounds.top !in 0..999 ||
                    bounds.bottom !in 1..1000 ||
                    bounds.left >= bounds.right ||
                    bounds.top >= bounds.bottom
            )
                return null
            val x0 =
                when (rotation) {
                    90 -> 1 - bottom
                    180 -> 1 - right
                    270 -> top
                    else -> left
                }
            val x1 =
                when (rotation) {
                    90 -> 1 - top
                    180 -> 1 - left
                    270 -> bottom
                    else -> right
                }
            val y0 =
                when (rotation) {
                    90 -> left
                    180 -> 1 - bottom
                    270 -> 1 - right
                    else -> top
                }
            val y1 =
                when (rotation) {
                    90 -> right
                    180 -> 1 - top
                    270 -> 1 - left
                    else -> bottom
                }
            val w = if (rotation % 180 == 0) width else height
            val h = if (rotation % 180 == 0) height else width
            val scale = minOf(viewWidth / w, viewHeight / h)
            val dx = (viewWidth - w * scale) / 2
            val dy = (viewHeight - h * scale) / 2
            return OverlayRectangle(
                dx + x0 * w * scale,
                dy + y0 * h * scale,
                (x1 - x0) * w * scale,
                (y1 - y0) * h * scale,
            )
        }
    }
}
