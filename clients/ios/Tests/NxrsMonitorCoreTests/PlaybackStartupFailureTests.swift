import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import NxrsMonitorCore

@MainActor private final class FailingStartupPeer: PlaybackStartupPeer {
    var hasDecodedVideo = false
    var failed = false
    var closed = false
    func completeOffer() async throws -> String { throw PlaybackStartupError.peerFailed }
    func apply(answer: String) async throws { XCTFail("Answer must not be applied") }
    func close() { closed = true }
}

private actor UnusedStartupHTTP: HTTPTransport {
    private(set) var calls = 0
    func send(_ request: URLRequest) async throws -> HTTPResult {
        calls += 1
        throw MonitorError.serviceUnavailable
    }
}

final class PlaybackStartupFailureTests: XCTestCase {
    @MainActor func testFailureNotificationIsOnceAndAfterLocalClose() async throws {
        let peer = FailingStartupPeer()
        let http = UnusedStartupHTTP()
        let policy = try EndpointPolicy(gateway: URL(string: "https://gateway.example.test")!)
        var notices = 0
        let owner = PlaybackStartup(peer: peer, api: GatewayClient(policy: policy, transport: http),
                                   whep: WHEPClient(policy: policy, transport: http)) { error in
            notices += 1
            XCTAssertTrue(peer.closed, "The UI must not observe failure while decoding is still active")
            XCTAssertEqual(error as? PlaybackStartupError, .peerFailed)
        }
        do {
            try await owner.start(cameraID: ResourceID("cam_test"), credential: BearerCredential("SYNTHETIC_VIEWER"))
            XCTFail("Expected peer failure")
        } catch { XCTAssertEqual(error as? PlaybackStartupError, .peerFailed) }
        await owner.close()
        XCTAssertEqual(notices, 1, "Repeated cleanup must not generate stale UI events")
        let calls = await http.calls
        XCTAssertEqual(calls, 0)
    }
}
