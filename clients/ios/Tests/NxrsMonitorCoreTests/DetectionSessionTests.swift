import Foundation
import XCTest
@testable import NxrsMonitorCore

private final class FeedFixture: DetectionFeedClient, @unchecked Sendable {
    private let lock = NSLock()
    private var feeds: [AsyncThrowingStream<DetectionMessage, Error>.Continuation] = []
    private var stopped = 0
    var stopCount: Int { lock.lock(); defer { lock.unlock() }; return stopped }
    func subscribe(cameraID: ResourceID, credential: BearerCredential, cursor: String?) -> DetectionSubscription {
        let stream = AsyncThrowingStream<DetectionMessage, Error>(bufferingPolicy: .bufferingNewest(1)) { c in
            lock.lock(); feeds.append(c); lock.unlock()
        }
        return DetectionSubscription(stream, cancel: { [weak self] in
            guard let self = self else { return }; self.lock.lock(); self.stopped += 1; self.lock.unlock()
        })
    }
    func emit(_ snapshot: DetectionSnapshot, index: Int) {
        lock.lock(); let c = feeds.indices.contains(index) ? feeds[index] : nil; lock.unlock()
        let now = ProcessInfo.processInfo.systemUptime
        c?.yield(DetectionMessage(snapshot: snapshot, cursor: "\(snapshot.source_session_id ?? "none"):1", receivedAt: now, requestStartedAt: now))
    }
}
private final class WeakSessionReference {
    weak var value: DetectionSession?
    init(_ value: DetectionSession?) { self.value = value }
}

@MainActor final class DetectionSessionTests: XCTestCase {
    func snapshot(camera: String) throws -> DetectionSnapshot {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "detection", withExtension: "json", subdirectory: "Fixtures"))
        var value = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        value["camera_id"] = camera
        return try JSONDecoder().decode(DetectionSnapshot.self, from: JSONSerialization.data(withJSONObject: value))
    }
    func testSwitchAndStopFenceLateCallbacksFromPriorCamera() async throws {
        let fixture = FeedFixture(); let session = DetectionSession(); let token = try BearerCredential("a")
        session.start(client: fixture, cameraID: try ResourceID("cam_one"), credential: token)
        try await Task.sleep(nanoseconds: 10_000_000)
        fixture.emit(try snapshot(camera: "cam_one"), index: 0)
        try await Task.sleep(nanoseconds: 10_000_000)
        XCTAssertEqual(session.presentation.presence(now: ProcessInfo.processInfo.systemUptime, videoLive: true), .person)
        session.start(client: fixture, cameraID: try ResourceID("other"), credential: token)
        try await Task.sleep(nanoseconds: 10_000_000)
        fixture.emit(try snapshot(camera: "cam_one"), index: 0)
        XCTAssertNil(session.presentation.snapshot)
        fixture.emit(try snapshot(camera: "other"), index: 1)
        try await Task.sleep(nanoseconds: 10_000_000)
        XCTAssertEqual(session.presentation.snapshot?.camera_id, "other")
        session.stop(); fixture.emit(try snapshot(camera: "other"), index: 1)
        try await Task.sleep(nanoseconds: 10_000_000)
        XCTAssertNil(session.presentation.snapshot); XCTAssertGreaterThanOrEqual(fixture.stopCount, 2)
    }
    func testDiscardingTheOwnerDoesNotRetainAnIdleSubscription() async throws {
        let fixture = FeedFixture(); var session: DetectionSession? = DetectionSession(); let reference = WeakSessionReference(session)
        session?.start(client: fixture, cameraID: try ResourceID("cam_one"), credential: try BearerCredential("a"))
        try await Task.sleep(nanoseconds: 10_000_000); session = nil
        try await Task.sleep(nanoseconds: 10_000_000)
        XCTAssertNil(reference.value); XCTAssertEqual(fixture.stopCount, 1)
    }
}
