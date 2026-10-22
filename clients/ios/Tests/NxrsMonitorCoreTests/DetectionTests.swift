import Foundation
import XCTest
@testable import NxrsMonitorCore

final class DetectionTests: XCTestCase {
    func fixture() throws -> Data {
        try Data(contentsOf: XCTUnwrap(Bundle.module.url(forResource: "detection", withExtension: "json", subdirectory: "Fixtures")))
    }
    func snapshot(_ change: (inout [String: Any]) -> Void = { _ in }) throws -> DetectionSnapshot {
        var value = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture()) as? [String: Any]); change(&value)
        return try JSONDecoder().decode(DetectionSnapshot.self, from: JSONSerialization.data(withJSONObject: value))
    }
    func testSharedSchemaAndNormalizedBoxes() throws {
        let s = try snapshot(); try s.validate(cameraID: ResourceID("cam_one"))
        XCTAssertEqual(s.source?.latest?.objects.first?.bounds.left, 100)
        XCTAssertEqual(s.source?.latest?.person_score_permille, 940)
        XCTAssertFalse(s.frame_synchronized)
    }
    func testFreshnessRequiresLiveVideoAndExpiresWithoutAnotherNetworkEvent() throws {
        let camera = try ResourceID("cam_one"); var state = DetectionState()
        try state.receive(DetectionMessage(snapshot: snapshot(), cursor: "unused", receivedAt: 10.1, requestStartedAt: 10), cameraID: camera)
        XCTAssertEqual(state.presence(now: 10.2, videoLive: true), .person)
        XCTAssertEqual(state.presence(now: 10.2, videoLive: false), .unavailable)
        XCTAssertEqual(state.presence(now: 11.6, videoLive: true), .unavailable)
        state.reset(); XCTAssertEqual(state.presence(now: 10.2, videoLive: true), .unavailable)
    }
    func testDelayedSSECannotMakeAnOldDetectionFreshAgain() throws {
        let camera = try ResourceID("cam_one"); var state = DetectionState()
        try state.receive(DetectionMessage(snapshot: snapshot(), cursor: "unused", receivedAt: 20, requestStartedAt: 10), cameraID: camera)
        XCTAssertEqual(state.presence(now: 20, videoLive: true), .unavailable)
        let future = try snapshot { $0["stream_elapsed_ms"] = 20000 }
        XCTAssertThrowsError(try state.receive(DetectionMessage(snapshot: future, cursor: "unused", receivedAt: 20, requestStartedAt: 10), cameraID: camera))
    }
    func testWrongCameraBadScoreGeometryAndExcessObjectsAreRejected() throws {
        let camera = try ResourceID("cam_one")
        XCTAssertThrowsError(try snapshot().validate(cameraID: ResourceID("other")))
        for kind in 0..<4 {
            let s = try snapshot { value in
                var source = value["source"] as! [String: Any]
                var latest = source["latest"] as! [String: Any]
                if kind == 0 { latest["person_score_permille"] = 1001 }
                if kind == 1 { latest["captured_ms"] = 1001 }
                if kind == 2 { latest["objects"] = Array(repeating: (latest["objects"] as! [Any])[0], count: 11) }
                if kind == 3 {
                    var objects = latest["objects"] as! [[String: Any]]
                    objects[0]["bounds"] = ["left": 900, "top": 0, "right": 10, "bottom": 100]
                    latest["objects"] = objects
                }
                source["latest"] = latest; value["source"] = source
            }
            XCTAssertThrowsError(try s.validate(cameraID: camera))
        }
    }
    func testSessionChangeInvalidatesPriorStateAndSameSessionRevisionCannotRegress() throws {
        let camera = try ResourceID("cam_one"); var state = DetectionState()
        try state.receive(DetectionMessage(snapshot: snapshot(), cursor: "", receivedAt: 10, requestStartedAt: 10), cameraID: camera)
        let old = try snapshot { $0["revision"] = 1 }
        XCTAssertThrowsError(try state.receive(DetectionMessage(snapshot: old, cursor: "", receivedAt: 10, requestStartedAt: 10), cameraID: camera))
        let reset = try snapshot { $0["source_session_id"] = String(repeating: "b", count: 32); $0["revision"] = 1
            $0["source_online"] = false; $0["presence"] = "unavailable"; $0["source"] = NSNull(); $0["observation_age_ms"] = NSNull() }
        try state.receive(DetectionMessage(snapshot: reset, cursor: "", receivedAt: 10, requestStartedAt: 10), cameraID: camera)
        XCTAssertEqual(state.presence(now: 10, videoLive: true), .unavailable)
    }
    func testFragmentedSSEHasBoundedFramingAndRejectsAmbiguousEvents() throws {
        let data = try JSONSerialization.data(withJSONObject: JSONSerialization.jsonObject(with: fixture()), options: [.sortedKeys])
        var bytes = Data("event: detection\r\nid: abc:1\r\ndata: ".utf8); bytes.append(data); bytes.append(Data("\r\n\r\n".utf8))
        var framer = DetectionFramer(); var found = 0
        for byte in bytes { try framer.append(Data([byte])) { id, payload in
            XCTAssertEqual(id, "abc:1"); XCTAssertEqual(payload, data); found += 1
        } }
        XCTAssertEqual(found, 1)
        var tooLong = DetectionFramer()
        XCTAssertThrowsError(try tooLong.append(Data(repeating: 65, count: 8193)) { _, _ in })
        var duplicate = DetectionFramer()
        XCTAssertThrowsError(try duplicate.append(Data("data: {}\ndata: {}\n\n".utf8)) { _, _ in })
    }
}
