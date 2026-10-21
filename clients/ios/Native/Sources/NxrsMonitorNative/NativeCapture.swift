import Foundation
import AVFoundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
@preconcurrency import WebRTC

public enum NativeCaptureError: Error, Equatable, Sendable {
    case noCurrentFrame, invalidFrame, encodingFailed, noRecordedFrames, alreadyFinished
}

/// RTC frames/buffers are immutable after decoder delivery. Retain one across a queue hop.
struct VideoFrameReference: @unchecked Sendable {
    let frame: RTCVideoFrame
    init(_ frame: RTCVideoFrame) { self.frame = frame }
}

enum NativeFrameImage {
    static func image(_ reference: VideoFrameReference) throws -> CIImage {
        let frame = reference.frame
        guard frame.width > 0, frame.height > 0, frame.width <= 4096, frame.height <= 4096 else {
            throw NativeCaptureError.invalidFrame
        }
        let pixelBuffer: CVPixelBuffer
        var image: CIImage
        if let native = frame.buffer as? RTCCVPixelBuffer {
            pixelBuffer = native.pixelBuffer
            image = CIImage(cvPixelBuffer: pixelBuffer)
            if native.requiresCropping() {
                image = image.cropped(to: CGRect(x: Int(native.cropX), y: Int(native.cropY),
                    width: Int(native.cropWidth), height: Int(native.cropHeight)))
                image = image.transformed(by: CGAffineTransform(translationX: -image.extent.minX, y: -image.extent.minY))
            }
        } else {
            let planar = frame.buffer.toI420()
            var created: CVPixelBuffer?
            guard CVPixelBufferCreate(kCFAllocatorDefault, Int(planar.width), Int(planar.height),
                kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &created) == kCVReturnSuccess,
                  let created else { throw NativeCaptureError.invalidFrame }
            pixelBuffer = created
            CVPixelBufferLockBaseAddress(pixelBuffer, [])
            defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, []) }
            guard let y = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 0),
                  let uv = CVPixelBufferGetBaseAddressOfPlane(pixelBuffer, 1) else {
                throw NativeCaptureError.invalidFrame
            }
            let yStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 0)
            let uvStride = CVPixelBufferGetBytesPerRowOfPlane(pixelBuffer, 1)
            for row in 0..<Int(planar.height) {
                memcpy(y.advanced(by: row * yStride), planar.dataY.advanced(by: row * Int(planar.strideY)), Int(planar.width))
            }
            for row in 0..<Int(planar.chromaHeight) {
                let output = uv.advanced(by: row * uvStride).assumingMemoryBound(to: UInt8.self)
                let u = planar.dataU.advanced(by: row * Int(planar.strideU))
                let v = planar.dataV.advanced(by: row * Int(planar.strideV))
                for column in 0..<Int(planar.chromaWidth) {
                    output[column * 2] = u[column]; output[column * 2 + 1] = v[column]
                }
            }
            image = CIImage(cvPixelBuffer: pixelBuffer)
        }
        image = image.transformed(by: CGAffineTransform(scaleX: CGFloat(frame.width) / image.extent.width,
                                                        y: CGFloat(frame.height) / image.extent.height))
        let orientation: Int32
        switch frame.rotation.rawValue {
        case 90: orientation = 6
        case 180: orientation = 3
        case 270: orientation = 8
        default: orientation = 1
        }
        image = image.oriented(forExifOrientation: orientation)
        return image.transformed(by: CGAffineTransform(translationX: -image.extent.minX, y: -image.extent.minY))
    }

    static func jpeg(_ frame: VideoFrameReference) throws -> Data {
        let image = try image(frame)
        let context = CIContext(options: [.cacheIntermediates: false])
        guard let data = context.jpegRepresentation(of: image, colorSpace: CGColorSpaceCreateDeviceRGB(),
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.9]) else {
            throw NativeCaptureError.encodingFailed
        }
        return data
    }
}

public struct NativeClipResult: Sendable {
    public let duration: TimeInterval
    public let frameCount: Int
}

/// One serial encoding queue and at most one pending decoder frame. Decoder delivery
/// never waits for disk or encoding. Clips contain video only, at most 60s/900 frames.
final class ClipWriter: NSObject, RTCVideoRenderer, @unchecked Sendable {
    let url: URL
    private let queue = DispatchQueue(label: "org.nxrs.home.clip-writer", qos: .userInitiated)
    private let lock = NSLock()
    private var accepting = true
    private var pending = false
    // All following state belongs to queue, including completion/abort barriers.
    private var writer: AVAssetWriter?
    private var input: AVAssetWriterInput?
    private var adaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var firstStamp: Int64?
    private var lastStamp: Int64?
    private var firstArrival: TimeInterval?
    private var frameCount = 0
    private var failure: Error?
    private let context = CIContext(options: [.cacheIntermediates: false])
    private var size = CGSize.zero

    init(url: URL) { self.url = url }
    func setSize(_ size: CGSize) {}
    func renderFrame(_ frame: RTCVideoFrame?) {
        guard let frame else { return }
        lock.lock()
        guard accepting, !pending else { lock.unlock(); return }
        pending = true
        lock.unlock()
        let reference = VideoFrameReference(frame)
        let arrival = ProcessInfo.processInfo.systemUptime
        queue.async { [self] in
            defer { lock.lock(); pending = false; lock.unlock() }
            guard failure == nil else { return }
            do { try autoreleasepool { try append(reference, arrival: arrival) } }
            catch { failure = error }
        }
    }

    private func append(_ reference: VideoFrameReference, arrival: TimeInterval) throws {
        let stamp = reference.frame.timeStampNs
        if let firstArrival, arrival - firstArrival >= 60 { return }
        if let firstStamp {
            let elapsed = stamp.subtractingReportingOverflow(firstStamp)
            if elapsed.overflow || elapsed.partialValue < 0 || elapsed.partialValue >= 60_000_000_000 { return }
        }
        if let lastStamp {
            let elapsed = stamp.subtractingReportingOverflow(lastStamp)
            if elapsed.overflow || elapsed.partialValue < 66_666_666 { return }
        } // At most 15 frames/s; malformed/wrapped timestamps cannot overflow.
        guard frameCount < 900 else { return }
        let image = try NativeFrameImage.image(reference)
        if writer == nil {
            size = CGSize(width: Int(image.extent.width) / 2 * 2, height: Int(image.extent.height) / 2 * 2)
            guard size.width > 0, size.height > 0 else { throw NativeCaptureError.invalidFrame }
            let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
            let settings: [String: Any] = [AVVideoCodecKey: AVVideoCodecType.h264,
                AVVideoWidthKey: Int(size.width), AVVideoHeightKey: Int(size.height),
                AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 2_000_000,
                                                  AVVideoMaxKeyFrameIntervalKey: 30]]
            let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
            input.expectsMediaDataInRealTime = true
            guard writer.canAdd(input) else { throw NativeCaptureError.encodingFailed }
            writer.add(input)
            adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input,
                sourcePixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                    kCVPixelBufferWidthKey as String: Int(size.width), kCVPixelBufferHeightKey as String: Int(size.height),
                    kCVPixelBufferIOSurfacePropertiesKey as String: [:]])
            guard writer.startWriting() else { throw NativeCaptureError.encodingFailed }
            writer.startSession(atSourceTime: .zero)
            self.writer = writer; self.input = input
            firstStamp = stamp; firstArrival = arrival
            #if os(iOS)
            try FileManager.default.setAttributes([.protectionKey: FileProtectionType.complete,
                                                   .posixPermissions: 0o600], ofItemAtPath: url.path)
            #endif
        }
        guard let input, input.isReadyForMoreMediaData, let adaptor,
              let pool = adaptor.pixelBufferPool, let firstStamp else { return }
        var pixel: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &pixel) == kCVReturnSuccess,
              let pixel else { throw NativeCaptureError.encodingFailed }
        let scaled = image.transformed(by: CGAffineTransform(scaleX: size.width / image.extent.width,
                                                             y: size.height / image.extent.height))
        context.render(scaled, to: pixel)
        guard adaptor.append(pixel, withPresentationTime: CMTime(value: stamp - firstStamp, timescale: 1_000_000_000)) else {
            throw NativeCaptureError.encodingFailed
        }
        frameCount += 1; lastStamp = stamp
        if frameCount.isMultiple(of: 30),
           let bytes = try FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber,
           bytes.intValue > 31 * 1024 * 1024 { throw NativeCaptureError.encodingFailed }
    }

    func finish() async throws -> NativeClipResult {
        stopAdmission()
        return try await withCheckedThrowingContinuation { continuation in
            queue.async { [self] in
                if let failure {
                    writer?.cancelWriting(); try? FileManager.default.removeItem(at: url)
                    continuation.resume(throwing: failure); return
                }
                guard let writer, let input, frameCount >= 2,
                      let firstStamp, let lastStamp else {
                    self.writer?.cancelWriting(); try? FileManager.default.removeItem(at: url)
                    continuation.resume(throwing: NativeCaptureError.noRecordedFrames); return
                }
                input.markAsFinished()
                let result = NativeClipResult(duration: min(60, max(0.01, Double(lastStamp - firstStamp) / 1_000_000_000)), frameCount: frameCount)
                writer.finishWriting {
                    if writer.status == .completed { continuation.resume(returning: result) }
                    else {
                        try? FileManager.default.removeItem(at: self.url)
                        continuation.resume(throwing: NativeCaptureError.encodingFailed)
                    }
                }
            }
        }
    }

    func abort() {
        stopAdmission()
        queue.async { [self] in
            if writer?.status == .writing { writer?.cancelWriting() }
            try? FileManager.default.removeItem(at: url)
        }
    }

    private func stopAdmission() {
        lock.lock(); accepting = false; lock.unlock()
    }
}

@MainActor public final class NativeClipRecorder {
    private weak var peer: NativeVideoPeer?
    private let renderer: ClipWriter
    private var finished = false

    init(peer: NativeVideoPeer, url: URL) throws {
        guard url.isFileURL, url.pathExtension == "mov", !FileManager.default.fileExists(atPath: url.path) else {
            throw NativeCaptureError.encodingFailed
        }
        self.peer = peer
        renderer = ClipWriter(url: url)
        peer.addRenderer(renderer)
    }

    public func finish() async throws -> NativeClipResult {
        guard !finished else { throw NativeCaptureError.alreadyFinished }
        finished = true
        peer?.removeRenderer(renderer)
        return try await renderer.finish()
    }

    deinit { if !finished { renderer.abort() } }
}
