import Foundation
import NxrsMonitorCore
@preconcurrency import WebRTC

/// Per-peer bounded associations. SDK decoder callbacks may outlive close; the
/// closed flag fences every callback without retaining a peer or a UIView.
final class VideoMetadataStore: @unchecked Sendable {
    private let lock = NSLock()
    private var active = true
    private var received: UInt64 = 0, parsed: UInt64 = 0, matched: UInt64 = 0
    var statistics: String { lock.lock(); defer { lock.unlock() }; return "received=\(received),parsed=\(parsed),matched=\(matched),pending=\(pending.count)" }
    private var pending: [UInt32: VideoDetectionFrame] = [:]
    // Retain eight immutable SDK buffers: prevents pool-address reuse while
    // renderer callbacks cross the SDK's timestamp-normalization boundary.
    private struct Decoded {
        let buffer: any RTCVideoFrameBuffer
        let metadata: VideoDetectionFrame?
    }
    private var decoded: [ObjectIdentifier: Decoded] = [:]
    private var order: [ObjectIdentifier] = []
    private func identity(_ frame: RTCVideoFrame) -> ObjectIdentifier {
        if let native = frame.buffer as? RTCCVPixelBuffer { return ObjectIdentifier(native.pixelBuffer) }
        return ObjectIdentifier(frame.buffer)
    }
    private var epoch: String?
    private var sequence: UInt64 = 0
    func encoded(_ image: RTCEncodedImage) {
        let value = try? VideoDetectionSEI.decode(image.buffer)
        lock.lock(); defer { lock.unlock() }
        guard active else { return }
        received += 1
        pending.removeValue(forKey: image.timeStamp)
        guard let value else { return }
        parsed += 1
        if epoch != value.epoch { pending.removeAll(); decoded.removeAll(); order.removeAll(); epoch = value.epoch; sequence = 0 }
        guard value.frame_sequence > sequence else { return }
        sequence = value.frame_sequence
        if pending.count >= 64 { pending.removeAll() }
        pending[image.timeStamp] = value
    }
    func completed(_ frame: RTCVideoFrame) {
        lock.lock(); defer { lock.unlock() }
        guard active else { return }
        let value = pending.removeValue(forKey: UInt32(bitPattern: frame.timeStamp))
        let key = identity(frame)
        if let previous = decoded[key] {
            // An SDK may alias immutable pixels across different frames. A
            // buffer alone then cannot identify which frame was rendered.
            decoded[key] = Decoded(buffer: previous.buffer, metadata: nil)
            return
        }
        guard let value else { return }
        matched += 1
        decoded[key] = Decoded(buffer: frame.buffer, metadata: value); order.append(key)
        if order.count > 8 { decoded.removeValue(forKey: order.removeFirst()) }
    }
    func metadata(_ frame: RTCVideoFrame) -> VideoDetectionFrame? {
        lock.lock(); defer { lock.unlock() }; return active ? decoded[identity(frame)]?.metadata : nil
    }
    func close() {
        lock.lock(); defer { lock.unlock() }; active = false; pending.removeAll(); decoded.removeAll(); order.removeAll()
    }
}

final class MetadataDecoderFactory: NSObject, RTCVideoDecoderFactory {
    private let base = RTCDefaultVideoDecoderFactory()
    private let store: VideoMetadataStore
    init(store: VideoMetadataStore) { self.store = store }
    func supportedCodecs() -> [RTCVideoCodecInfo] { base.supportedCodecs() }
    func createDecoder(_ info: RTCVideoCodecInfo) -> (any RTCVideoDecoder)? {
        guard let decoder = base.createDecoder(info) else { return nil }
        return info.name.uppercased() == "H264" ? MetadataDecoder(base: decoder, store: store) : decoder
    }
}
private final class MetadataDecoder: NSObject, RTCVideoDecoder {
    private let base: any RTCVideoDecoder
    private let store: VideoMetadataStore
    init(base: any RTCVideoDecoder, store: VideoMetadataStore) { self.base = base; self.store = store }
    func setCallback(_ callback: @escaping RTCVideoDecoderCallback) {
        base.setCallback { [store] frame in store.completed(frame); callback(frame) }
    }
    func startDecode(withNumberOfCores numberOfCores: Int32) -> Int { base.startDecode(withNumberOfCores: numberOfCores) }
    func release() -> Int { base.release() }
    func decode(_ image: RTCEncodedImage, missingFrames: Bool, codecSpecificInfo: (any RTCCodecSpecificInfo)?, renderTimeMs: Int64) -> Int {
        store.encoded(image)
        return base.decode(image, missingFrames: missingFrames, codecSpecificInfo: codecSpecificInfo, renderTimeMs: renderTimeMs)
    }
    func implementationName() -> String { base.implementationName() }
}
