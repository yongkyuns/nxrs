import XCTest
import AVFoundation
import ImageIO
@preconcurrency import WebRTC
@testable import NxrsMonitorNative

final class NativeCaptureTests: XCTestCase {
    private func frame(stamp: Int64 = 0, rotation: Int = 0) throws -> RTCVideoFrame {
        var buffer: CVPixelBuffer?
        XCTAssertEqual(CVPixelBufferCreate(kCFAllocatorDefault, 64, 48, kCVPixelFormatType_32BGRA,
            [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &buffer), kCVReturnSuccess)
        let pixel = try XCTUnwrap(buffer)
        CVPixelBufferLockBaseAddress(pixel, [])
        if let bytes = CVPixelBufferGetBaseAddress(pixel) {
            memset(bytes, 180, CVPixelBufferGetBytesPerRow(pixel) * 48)
        }
        CVPixelBufferUnlockBaseAddress(pixel, [])
        return RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: pixel),
            rotation: RTCVideoRotation(rawValue: rotation)!, timeStampNs: stamp)
    }

    func testSnapshotIsARealJPEGWithRotationPreserved() throws {
        let data = try NativeFrameImage.jpeg(VideoFrameReference(frame(rotation: 90)))
        let image = try XCTUnwrap(CGImageSourceCreateWithData(data as CFData, nil))
        let decoded = try XCTUnwrap(CGImageSourceCreateImageAtIndex(image, 0, nil))
        XCTAssertEqual(decoded.width, 48)
        XCTAssertEqual(decoded.height, 64)
        XCTAssertTrue(data.starts(with: [0xff, 0xd8]))
    }

    func testPlanarDecoderFramesProduceARealJPEG() throws {
        let buffer = RTCMutableI420Buffer(width: 64, height: 48)
        memset(buffer.mutableDataY, 180, Int(buffer.strideY * buffer.height))
        memset(buffer.mutableDataU, 128, Int(buffer.strideU * buffer.chromaHeight))
        memset(buffer.mutableDataV, 128, Int(buffer.strideV * buffer.chromaHeight))
        let frame = RTCVideoFrame(buffer: buffer, rotation: ._0, timeStampNs: 0)
        let data = try NativeFrameImage.jpeg(VideoFrameReference(frame))
        let source = try XCTUnwrap(CGImageSourceCreateWithData(data as CFData, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertEqual(image.width, 64)
        XCTAssertEqual(image.height, 48)
        XCTAssertGreaterThan(data.count, 100)
    }

    func testAdaptedNativeFrameCapturesTheSameTopCropAsWebRTC() throws {
        let original = try frame()
        let source = try XCTUnwrap(original.buffer as? RTCCVPixelBuffer).pixelBuffer
        CVPixelBufferLockBaseAddress(source, [])
        let base = try XCTUnwrap(CVPixelBufferGetBaseAddress(source)).assumingMemoryBound(to: UInt8.self)
        let stride = CVPixelBufferGetBytesPerRow(source)
        // WebRTC cropY starts at the top: red top half, blue bottom half.
        for row in 0..<48 {
            for column in 0..<64 {
                let offset = row * stride + column * 4
                base[offset] = row < 24 ? 0 : 255
                base[offset + 1] = 0
                base[offset + 2] = row < 24 ? 255 : 0
                base[offset + 3] = 255
            }
        }
        CVPixelBufferUnlockBaseAddress(source, [])
        let cropped = RTCCVPixelBuffer(pixelBuffer: source, adaptedWidth: 32, adaptedHeight: 16,
                                      cropWidth: 32, cropHeight: 16, cropX: 16, cropY: 0)
        let frame = RTCVideoFrame(buffer: cropped, rotation: ._0, timeStampNs: 0)
        let jpeg = try NativeFrameImage.jpeg(VideoFrameReference(frame))
        let container = try XCTUnwrap(CGImageSourceCreateWithData(jpeg as CFData, nil))
        let decoded = try XCTUnwrap(CGImageSourceCreateImageAtIndex(container, 0, nil))
        XCTAssertEqual(decoded.width, 32)
        XCTAssertEqual(decoded.height, 16)
        var rgba = [UInt8](repeating: 0, count: 4)
        try rgba.withUnsafeMutableBytes { bytes in
            let context = try XCTUnwrap(CGContext(data: bytes.baseAddress, width: 1, height: 1,
                bitsPerComponent: 8, bytesPerRow: 4, space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
            context.draw(decoded, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        }
        XCTAssertGreaterThan(rgba[0], 200, "The selected top crop must stay red")
        XCTAssertLessThan(rgba[2], 50, "The blue bottom half must not enter the top crop")
    }

    func testClipProducesDecodableVideoAndRejectsFramesBeyondLimit() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mov")
        defer { try? FileManager.default.removeItem(at: url) }
        let writer = ClipWriter(url: url)
        // Exercise the real SDK encoder; callback queue backpressure may drop frames.
        for index in 0..<24 {
            writer.renderFrame(try frame(stamp: Int64(index) * 100_000_000))
            try await Task.sleep(nanoseconds: 60_000_000)
        }
        writer.renderFrame(try frame(stamp: 61_000_000_000))
        try await Task.sleep(nanoseconds: 30_000_000)
        let result = try await writer.finish()
        XCTAssertGreaterThanOrEqual(result.frameCount, 2)
        XCTAssertLessThan(result.duration, 3)
        let asset = AVURLAsset(url: url)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        let track = try XCTUnwrap(tracks.first)
        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: track,
            outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
        reader.add(output)
        XCTAssertTrue(reader.startReading())
        let sample = try XCTUnwrap(output.copyNextSampleBuffer())
        let decoded = try XCTUnwrap(CMSampleBufferGetImageBuffer(sample))
        XCTAssertEqual(CVPixelBufferGetWidth(decoded), 64)
        XCTAssertEqual(CVPixelBufferGetHeight(decoded), 48)
        reader.cancelReading()
    }

    func testEmptyClipCannotBePresentedAsSavedVideo() async throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mov")
        let writer = ClipWriter(url: url)
        do { _ = try await writer.finish(); XCTFail("empty recording") }
        catch { XCTAssertEqual(error as? NativeCaptureError, .noRecordedFrames) }
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }

    func testEncoderFailureIsObservableAndFinishesWithoutPublishingAClip() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let url = directory.appendingPathComponent("unwritable.mov")
        let writer = ClipWriter(url: url) // Parent deliberately does not exist.
        writer.renderFrame(try frame())
        let deadline = ContinuousClock.now.advanced(by: .seconds(3))
        while !writer.hasFailed && ContinuousClock.now < deadline {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertTrue(writer.hasFailed, "The owner must be able to end failed recording promptly")
        do { _ = try await writer.finish(); XCTFail("failed encoder published a clip") }
        catch { XCTAssertFalse(FileManager.default.fileExists(atPath: url.path)) }
    }
}
