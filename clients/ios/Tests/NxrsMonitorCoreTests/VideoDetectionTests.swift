import XCTest
@testable import NxrsMonitorCore

final class VideoDetectionTests: XCTestCase {
    private func fixture(_ name: String, _ ext: String) throws -> Data {
        try Data(contentsOf: XCTUnwrap(Bundle.module.url(forResource: name, withExtension: ext, subdirectory: "Fixtures")))
    }
    func testRustSeiRoundTripAcrossAnnexBAvccAndUnrelatedEncoderMessages() throws {
        let bytes = try fixture("video-detection-sei", "bin")
        let frame = try XCTUnwrap(VideoDetectionSEI.decode(bytes))
        XCTAssertEqual(frame.frame_sequence, 20); XCTAssertEqual(frame.boxes.count, 1)
        XCTAssertEqual(frame.source?.latest?.frame_sequence, 12)
        XCTAssertEqual(frame.remainingLifetime, 1.2, accuracy: 0.001)
        XCTAssertEqual(frame.presence(), .person)
        XCTAssertEqual(frame.presence(additionalAge: 5), .unavailable)
        XCTAssertEqual(frame.presence(additionalAge: .nan), .unavailable)
        let nal = bytes.dropFirst(4); let n = UInt32(nal.count)
        var avcc = Data([UInt8(n >> 24), UInt8((n >> 16) & 255), UInt8((n >> 8) & 255), UInt8(n & 255)])
        avcc.append(contentsOf: nal)
        XCTAssertEqual(try VideoDetectionSEI.decode(avcc)?.frame_sequence, 20)
        var multiple = Data([0,0,0,1,6,5,16]); multiple.append(Data(repeating: 17,count: 16)); multiple.append(128); multiple.append(bytes)
        XCTAssertEqual(try VideoDetectionSEI.decode(multiple)?.frame_sequence, 20)
        XCTAssertThrowsError(try VideoDetectionSEI.decode(bytes + bytes), "Duplicate identities cannot attach ambiguously")
        XCTAssertThrowsError(try VideoDetectionSEI.decode(Data(repeating: 0,count: 1_048_577)))
        XCTAssertThrowsError(try VideoDetectionSEI.decode(Data([0,0,0,30,6])))
    }
    func testEmptyExpiredFutureAndUnconfirmedObservationsCannotDrawStaleBoxes() throws {
        let data = try fixture("video-detection", "json")
        let original = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        for change in 0..<4 {
            var value = original
            var source = value["source"] as! [String:Any]
            var latest = source["latest"] as! [String:Any]
            switch change {
            case 0: latest["objects"] = []; source["latest"] = latest
            case 1: value["frame_capture_ms"] = 2400
            case 2: source["presence"] = "unavailable"
            default: source["presence"] = "clear"
            }
            value["source"] = source
            let frame = try JSONDecoder().decode(VideoDetectionFrame.self, from: JSONSerialization.data(withJSONObject: value))
            try frame.validate(); XCTAssertTrue(frame.boxes.isEmpty); XCTAssertEqual(frame.remainingLifetime, 0)
        }
        var bad = original; bad["frame_sequence"] = 1
        XCTAssertThrowsError(try JSONDecoder().decode(VideoDetectionFrame.self, from: JSONSerialization.data(withJSONObject: bad)).validate())
    }
    func testRotationAndLetterboxTransformsUseTheEntireDisplayedSource() throws {
        let box = DetectionBounds(left: 100,top: 200,right: 300,bottom: 400)
        let unrotated = try XCTUnwrap(OverlayRectangle.fit(box,width: 320,height: 240,rotation: 0,viewWidth: 400,viewHeight: 400))
        XCTAssertEqual(unrotated.x,40,accuracy: 0.001); XCTAssertEqual(unrotated.y,110,accuracy: 0.001)
        XCTAssertEqual(unrotated.width,80,accuracy: 0.001); XCTAssertEqual(unrotated.height,60,accuracy: 0.001)
        let rotated = try XCTUnwrap(OverlayRectangle.fit(box,width: 320,height: 240,rotation: 90,viewWidth: 400,viewHeight: 400))
        XCTAssertEqual(rotated.x,230,accuracy: 0.001); XCTAssertEqual(rotated.y,40,accuracy: 0.001)
        XCTAssertEqual(rotated.width,60,accuracy: 0.001); XCTAssertEqual(rotated.height,80,accuracy: 0.001)
        for rotation in [0,90,180,270] {
            let r = try XCTUnwrap(OverlayRectangle.fit(box,width: 320,height: 240,rotation: rotation,viewWidth: 800,viewHeight: 300))
            XCTAssertGreaterThanOrEqual(r.x,0); XCTAssertGreaterThanOrEqual(r.y,0)
            XCTAssertLessThanOrEqual(r.x+r.width,800); XCTAssertLessThanOrEqual(r.y+r.height,300)
        }
        XCTAssertNil(OverlayRectangle.fit(box,width: 320,height: 240,rotation: 45,viewWidth: 400,viewHeight: 400))
    }
}
