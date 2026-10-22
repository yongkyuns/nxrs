import XCTest
import CoreVideo
@preconcurrency import WebRTC
@testable import NxrsMonitorNative

final class VideoMetadataTests: XCTestCase {
    private func payload() throws -> Data {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        return try Data(contentsOf: root.appendingPathComponent("../../../Tests/NxrsMonitorCoreTests/Fixtures/video-detection-sei.bin"))
    }
    private func image(stamp: UInt32) throws -> RTCEncodedImage {
        let image = RTCEncodedImage(); image.timeStamp = stamp; image.buffer = try payload(); return image
    }
    private func pixel() throws -> CVPixelBuffer {
        var value: CVPixelBuffer?
        XCTAssertEqual(CVPixelBufferCreate(kCFAllocatorDefault,64,48,kCVPixelFormatType_32BGRA,nil,&value),kCVReturnSuccess)
        return try XCTUnwrap(value)
    }
    func testDecodedBufferPreservesIdentityWhenSdkNormalizesRendererTimestamps() throws {
        let store = VideoMetadataStore(); let pixel = try pixel()
        store.encoded(try image(stamp: 1234))
        let decoded = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: pixel),rotation: ._0,timeStampNs: 0)
        decoded.timeStamp = 1234; store.completed(decoded)
        let rendered = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: pixel),rotation: ._0,timeStampNs: 999_000_000)
        rendered.timeStamp = 0
        XCTAssertEqual(store.metadata(rendered)?.frame_sequence,20)
        let unrelated = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: try self.pixel()),rotation: ._0,timeStampNs: 999_000_000)
        XCTAssertNil(store.metadata(unrelated), "Similar clocks do not identify the same decoded image")
        store.close(); XCTAssertNil(store.metadata(rendered))
        store.encoded(try image(stamp: 1234)); store.completed(decoded)
        XCTAssertNil(store.metadata(rendered), "Late callbacks cannot revive a closed peer")
    }
    func testMissingMetadataCannotReuseAnEarlierFrameAndReplayCannotReplaceItsIdentity() throws {
        let store = VideoMetadataStore()
        store.encoded(try image(stamp: 1234))
        let frame = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: try pixel()),rotation: ._0,timeStampNs: 1)
        frame.timeStamp = 999; store.completed(frame)
        XCTAssertNil(store.metadata(frame))
        let missing = RTCEncodedImage(); missing.timeStamp = 1234; missing.buffer = Data([0,0,0,1,0x65,0x88])
        store.encoded(missing); frame.timeStamp = 1234; store.completed(frame)
        XCTAssertNil(store.metadata(frame), "A frame without SEI clears a prior association at the same RTP timestamp")
        store.encoded(try image(stamp: 1235)); frame.timeStamp = 1235; store.completed(frame)
        XCTAssertNil(store.metadata(frame), "The same source-frame identity is not accepted a second time")
    }
    func testAliasedBuffersAreAmbiguousAndCannotCarryAnotherFramesBoxes() throws {
        let store = VideoMetadataStore(); let pixel = try pixel()
        let decoded = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: pixel),rotation: ._0,timeStampNs: 0)
        store.encoded(try image(stamp: 100)); decoded.timeStamp = 100; store.completed(decoded)
        XCTAssertNotNil(store.metadata(decoded))
        var next = try payload()
        let range = try XCTUnwrap(next.range(of: Data("\"frame_sequence\":20".utf8)))
        next.replaceSubrange(range, with: Data("\"frame_sequence\":21".utf8))
        let encoded = RTCEncodedImage(); encoded.timeStamp = 101; encoded.buffer = next
        store.encoded(encoded); decoded.timeStamp = 101; store.completed(decoded)
        XCTAssertNil(store.metadata(decoded), "Aliased pixels cannot justify a temporal association")
    }
    func testUnannotatedAliasCannotReusePreviousBoxes() throws {
        let store = VideoMetadataStore()
        let frame = RTCVideoFrame(buffer: RTCCVPixelBuffer(pixelBuffer: try pixel()),rotation: ._0,timeStampNs: 0)
        store.encoded(try image(stamp: 100)); frame.timeStamp = 100; store.completed(frame)
        XCTAssertNotNil(store.metadata(frame))
        let missing = RTCEncodedImage(); missing.timeStamp = 101; missing.buffer = Data([0,0,0,1,0x65,0x88])
        store.encoded(missing); frame.timeStamp = 101; store.completed(frame)
        XCTAssertNil(store.metadata(frame))
    }

}
