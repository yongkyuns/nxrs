package org.nxrs.monitor.media

import java.util.concurrent.atomic.AtomicReference
import org.nxrs.monitor.core.VideoDetectionFrame
import org.nxrs.monitor.core.VideoDetectionSei
import org.webrtc.*

class MetadataOwner {
    val releaseFailed = java.util.concurrent.atomic.AtomicBoolean(false)

    private enum class State {
        Active,
        Ambiguous,
        Closed,
    }

    val encoded = java.util.concurrent.atomic.AtomicLong()
    val decoded = java.util.concurrent.atomic.AtomicLong()
    private val state = AtomicReference(State.Active)
    private val epoch = AtomicReference<String?>(null)
    val open
        get() = state.get() != State.Closed

    val qualified
        get() = state.get() == State.Active

    val needsReplacement
        get() = state.get() == State.Ambiguous

    fun selectEpoch(value: String) {
        epoch.set(value)
    }

    fun accepts(value: VideoDetectionFrame) = qualified && epoch.get() == value.epoch

    fun invalidate() {
        state.compareAndSet(State.Active, State.Ambiguous)
    }

    fun close() {
        state.set(State.Closed)
    }
}

interface TaggedBuffer {
    val owner: MetadataOwner
    val annotation: VideoDetectionFrame?
}

private open class FrameBuffer(
    private val base: VideoFrame.Buffer,
    final override val owner: MetadataOwner,
    private val value: VideoDetectionFrame?,
) : VideoFrame.Buffer, TaggedBuffer {
    override val annotation
        get() = if (value != null && owner.accepts(value)) value else null

    override fun getWidth() = base.width

    override fun getHeight() = base.height

    override fun toI420() = base.toI420()

    override fun retain() = base.retain()

    override fun release() = base.release()

    // kNative preserves this exact Java object through the SDK's JNI round trip.
    override fun getBufferType() = 0

    override fun cropAndScale(
        x: Int,
        y: Int,
        width: Int,
        height: Int,
        scaledWidth: Int,
        scaledHeight: Int,
    ): VideoFrame.Buffer {
        val metadata =
            if (x == 0 && y == 0 && width == base.width && height == base.height) annotation
            else null
        return tagged(
            base.cropAndScale(x, y, width, height, scaledWidth, scaledHeight),
            owner,
            metadata,
        )
    }
}

internal fun tagged(
    base: VideoFrame.Buffer,
    owner: MetadataOwner,
    value: VideoDetectionFrame?,
): VideoFrame.Buffer = FrameBuffer(base, owner, value)

/**
 * Per-peer encoded-to-decoded association. No retained pixel buffers or clocks are used to identify
 * a rendered image. Tags travel with the SDK buffer.
 */
class MetadataDecoderFactory(val owner: MetadataOwner, private val output: (VideoFrame) -> Unit) :
    VideoDecoderFactory {
    // Byte-buffer output binds pixels to MediaCodec's output PTS. The SDK's
    // SurfaceTexture adapter replaces the image timestamp with a queued value;
    // that path failed the independent alternating-pixel identity witness.
    private val hardware = HardwareVideoDecoderFactory(null)
    private val software = PlatformSoftwareVideoDecoderFactory(null)

    private data class Entry(
        val originalTimestamp: Long,
        val metadata: VideoDetectionFrame?,
        val width: Int,
        val height: Int,
    )

    private val pending = LinkedHashMap<Long, Entry>()
    private var token = 0L
    private var decoded = 0L
    private var epoch: String? = null
    private val priorEpochs = mutableSetOf<String>()
    private var sequence = 0L
    private var width = 0
    private var height = 0

    override fun getSupportedCodecs(): Array<VideoCodecInfo> =
        (hardware.supportedCodecs.toList() + software.supportedCodecs.toList())
            .filter { it.name.equals("H264", true) }
            .distinctBy { it.name to it.params }
            .toTypedArray()

    override fun createDecoder(info: VideoCodecInfo): VideoDecoder? {
        if (!info.name.equals("H264", true)) return null
        val base = hardware.createDecoder(info) ?: software.createDecoder(info) ?: return null
        return object : VideoDecoder {
            override fun initDecode(
                settings: VideoDecoder.Settings,
                callback: VideoDecoder.Callback,
            ): VideoCodecStatus {
                if (settings.width.toLong() * settings.height > 2073600) {
                    owner.invalidate()
                    return VideoCodecStatus.ERR_PARAMETER
                }
                synchronized(pending) {
                    width = settings.width
                    height = settings.height
                }
                return base.initDecode(settings) { frame, time, qp ->
                    owner.decoded.incrementAndGet()
                    val entry =
                        synchronized(pending) {
                            if (frame.timestampNs <= decoded) {
                                owner.invalidate()
                                null
                            } else {
                                decoded = frame.timestampNs
                                val found = pending.remove(frame.timestampNs)
                                // Retire dropped codec inputs. A later output for one
                                // of those tokens is rejected, never attached by order.
                                pending.keys.removeAll { it < decoded }
                                if (found == null) owner.invalidate()
                                found
                            }
                        }
                    if (entry != null && owner.open) {
                        frame.buffer.retain()
                        val annotation =
                            entry.metadata?.takeIf {
                                entry.width > 0 &&
                                    entry.height > 0 &&
                                    frame.buffer.width == entry.width &&
                                    frame.buffer.height == entry.height
                            }
                        val result =
                            VideoFrame(
                                tagged(frame.buffer, owner, annotation),
                                frame.rotation,
                                entry.originalTimestamp,
                            )
                        try {
                            // A video-only monitor consumes the exact decoder
                            // output before the SDK's A/V playout smoothing can
                            // coalesce timely frames with aliased RTP timing.
                            output(result)
                            callback.onDecodedFrame(result, time, qp)
                        } finally {
                            result.release()
                        }
                    }
                }
            }

            override fun decode(
                frame: EncodedImage,
                info: VideoDecoder.DecodeInfo?,
            ): VideoCodecStatus {
                if (frame.encodedWidth.toLong() * frame.encodedHeight > 2073600) {
                    owner.invalidate()
                    return VideoCodecStatus.ERR_PARAMETER
                }
                owner.encoded.incrementAndGet()
                val buffer = frame.buffer.duplicate()
                val value =
                    if (buffer.remaining() <= 1048576)
                        try {
                            val bytes = ByteArray(buffer.remaining())
                            buffer.get(bytes)
                            VideoDetectionSei.decode(bytes)
                        } catch (_: Exception) {
                            null
                        }
                    else null
                val identity =
                    synchronized(pending) {
                        if (pending.size >= 64 || token > Long.MAX_VALUE - 1000) {
                            owner.invalidate()
                            pending.clear()
                        }
                        token += 1000 // A unique microsecond-aligned MediaCodec PTS.
                        var accepted: VideoDetectionFrame? = null
                        if (owner.qualified && value != null) {
                            if (epoch != value.epoch) {
                                if (value.epoch in priorEpochs || priorEpochs.size >= 8)
                                    owner.invalidate()
                                else {
                                    epoch?.let(priorEpochs::add)
                                    epoch = value.epoch
                                    sequence = 0
                                    owner.selectEpoch(value.epoch)
                                }
                            }
                            if (owner.qualified && value.sequence > sequence) {
                                sequence = value.sequence
                                accepted = value
                            }
                        }
                        if (frame.encodedWidth > 0 && frame.encodedHeight > 0) {
                            width = frame.encodedWidth
                            height = frame.encodedHeight
                        }
                        pending[token] = Entry(frame.captureTimeNs, accepted, width, height)
                        token
                    }
                // WebRTC's Java capture timestamp is only RTP / 90 (whole
                // milliseconds) and can alias during bursts. It is not an image
                // identity. Give the codec a private token and restore the SDK's
                // original timestamp only after looking up the actual output.
                frame.retain()
                val input =
                    EncodedImage.builder()
                        .setBuffer(frame.buffer.duplicate(), frame::release)
                        .setEncodedWidth(frame.encodedWidth)
                        .setEncodedHeight(frame.encodedHeight)
                        .setCaptureTimeNs(identity)
                        .setFrameType(frame.frameType)
                        .setRotation(frame.rotation)
                        .setQp(frame.qp)
                        .createEncodedImage()
                val status =
                    try {
                        base.decode(input, info)
                    } finally {
                        input.release()
                    }
                if (status != VideoCodecStatus.OK && status != VideoCodecStatus.NO_OUTPUT)
                    synchronized(pending) { pending.remove(identity) }
                return status
            }

            override fun release(): VideoCodecStatus {
                synchronized(pending) { pending.clear() }
                return base.release().also {
                    if (it != VideoCodecStatus.OK) owner.releaseFailed.set(true)
                }
            }

            override fun getImplementationName() =
                "source-frame metadata / " + base.implementationName
        }
    }
}
