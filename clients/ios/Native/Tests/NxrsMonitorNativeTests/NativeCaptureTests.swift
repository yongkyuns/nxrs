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
}
