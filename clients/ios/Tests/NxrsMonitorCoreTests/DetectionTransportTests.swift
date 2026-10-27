import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import NxrsMonitorCore

private final class DetectionProtocol: URLProtocol {
    enum Mode { case good, denied, wrongCamera, oversized, pending, wrongContent }
    private static let lock = NSLock()
    private static var mode = Mode.good
    private static var payload = Data()
    private static var stopped = 0
    static func configure(_ mode: Mode, _ payload: Data) {
        lock.lock(); defer { lock.unlock() }; Self.mode = mode; Self.payload = payload; stopped = 0
    }
    static var stopCount: Int { lock.lock(); defer { lock.unlock() }; return stopped }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock(); let mode = Self.mode; let payload = Self.payload; Self.lock.unlock()
        if case .pending = mode { return }
        let response = HTTPURLResponse(url: request.url!, statusCode: mode == .denied ? 403 : 200,
            httpVersion: "HTTP/1.1", headerFields: ["Content-Type": mode == .wrongContent ? "application/json" : "text/event-stream"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if mode == .oversized {
            client?.urlProtocol(self, didLoad: Data(repeating: 65, count: 65537))
        } else if mode == .good || mode == .wrongCamera {
            var bytes = Data("event: detection\nid: \(String(repeating: "a", count: 32)):1\ndata: ".utf8)
            bytes.append(payload); bytes.append(Data("\n\n".utf8))
            // Split within JSON and SSE field names, not just at event boundaries.
            for start in stride(from: 0, to: bytes.count, by: 7) {
                client?.urlProtocol(self, didLoad: bytes.subdata(in: start..<min(start + 7, bytes.count)))
            }
        }
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() { Self.lock.lock(); Self.stopped += 1; Self.lock.unlock() }
}

final class DetectionTransportTests: XCTestCase {
    private func configure(_ mode: DetectionProtocol.Mode) throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "detection", withExtension: "json", subdirectory: "Fixtures"))
        var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        if mode == .wrongCamera { object["camera_id"] = "other" }
        DetectionProtocol.configure(mode, try JSONSerialization.data(withJSONObject: object))
    }
    private func feed(_ url: String = "https://fixture.test/v1/cameras/cam_one/detections") throws -> DetectionSubscription {
        DetectionTransfer.open(URLRequest(url: URL(string: url)!), cameraID: try ResourceID("cam_one"), protocols: [DetectionProtocol.self])
    }
    func testRealDelegateDecodesFragmentedSharedFixture() async throws {
        try configure(.good); var count = 0
        for try await message in try feed() { count += 1; XCTAssertEqual(message.snapshot.presence, .person) }
        XCTAssertEqual(count, 1)
    }
    func testAuthorizationContentOriginAndSizeFailuresStopTheFeed() async throws {
        for (mode, expected) in [(DetectionProtocol.Mode.denied, MonitorError.forbidden), (.wrongCamera, .invalidContract),
                                 (.oversized, .responseTooLarge), (.wrongContent, .invalidContract)] {
            try configure(mode)
            do { for try await _ in try feed() { XCTFail("unexpected event") }; XCTFail("expected failure") }
            catch { XCTAssertEqual(error as? MonitorError, expected) }
        }
        do { for try await _ in try feed("http://fixture.test") {}; XCTFail("expected HTTPS policy") }
        catch { XCTAssertEqual(error as? MonitorError, .invalidEndpoint) }
    }
    func testCancellingPendingFeedRetiresTheURLSession() async throws {
        try configure(.pending)
        let task = Task { for try await _ in try feed() {} }
        try await Task.sleep(nanoseconds: 20_000_000); task.cancel()
        _ = try? await task.value
        for _ in 0..<20 {
            if DetectionProtocol.stopCount > 0 { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(DetectionProtocol.stopCount, 1)
    }
}
