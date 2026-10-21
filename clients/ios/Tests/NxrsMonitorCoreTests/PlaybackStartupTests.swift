import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import NxrsMonitorCore

private let startupSDP = "v=0\r\ns=synthetic startup test\r\n"

@MainActor private final class StartupPeer: PlaybackStartupPeer {
    var hasDecodedVideo = false
    var failed = false
    var closed = false
    var offerCalls = 0
    var answerCalls = 0
    var autoFrame = true
    var offerDelay: Duration = .zero
    var answerDelay: Duration = .zero
    var ignoresOfferCancellation = false
    var closedBeforeLateOffer = false
    var answerFails = false
    var canceledPhase: String?

    func completeOffer() async throws -> String {
        offerCalls += 1
        do { try await Task.sleep(for: offerDelay) }
        catch {
            canceledPhase = "offer"
            if !ignoresOfferCancellation { throw error }
            closedBeforeLateOffer = closed
        }
        return startupSDP
    }
    func apply(answer: String) async throws {
        answerCalls += 1
        do { try await Task.sleep(for: answerDelay) }
        catch { canceledPhase = "answer"; throw error }
        if answerFails { throw PlaybackStartupError.peerFailed }
        if autoFrame { hasDecodedVideo = true }
    }
    func close() { closed = true; hasDecodedVideo = false }
}

private actor StartupHTTP: HTTPTransport {
    var grantDelay: Duration = .zero
    var postDelay: Duration = .zero
    var ignorePostCancellation = false
    var grantStatus = 201
    var deleteStatus = 204
    private(set) var grants = 0
    private(set) var posts = 0
    private(set) var deletes = 0
    private(set) var canceledPhase: String?
    private(set) var deleteWasCancelled = false

    func configure(grantDelay: Duration = .zero, postDelay: Duration = .zero,
                   ignorePostCancellation: Bool = false, grantStatus: Int = 201, deleteStatus: Int = 204) {
        self.grantDelay = grantDelay; self.postDelay = postDelay
        self.ignorePostCancellation = ignorePostCancellation
        self.grantStatus = grantStatus; self.deleteStatus = deleteStatus
    }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        if request.httpMethod == "DELETE" {
            deletes += 1
            deleteWasCancelled = Task.isCancelled
            return result(request, status: deleteStatus)
        }
        if request.url!.path.hasSuffix("playback-grants") {
            grants += 1
            do { try await Task.sleep(for: grantDelay) }
            catch { canceledPhase = "grant"; throw error }
            return result(request, status: grantStatus, body: """
                {"api_version":"1","camera_id":"cam_test","protocol":"whep",
                 "endpoint":"https://gateway.example.test/reference/whep",
                 "access_token":"SYNTHETIC_MEDIA_TOKEN","token_type":"Bearer","expires_at":1060}
                """)
        }
        posts += 1
        do { try await Task.sleep(for: postDelay) }
        catch {
            canceledPhase = "post"
            if !ignorePostCancellation { throw error }
        }
        return result(request, status: 201, body: startupSDP,
                      headers: ["Content-Type": "application/sdp", "Location": "/reference/whep/test_session"])
    }
    private func result(_ request: URLRequest, status: Int, body: String = "",
                        headers: [String: String] = ["Content-Type": "application/json"]) -> HTTPResult {
        HTTPResult(data: Data(body.utf8), response: HTTPURLResponse(url: request.url!, statusCode: status,
            httpVersion: nil, headerFields: headers)!)
    }
}

final class PlaybackStartupTests: XCTestCase {
    @MainActor private func startup(_ peer: StartupPeer, _ http: StartupHTTP) throws -> PlaybackStartup {
        let policy = try EndpointPolicy(gateway: URL(string: "https://gateway.example.test")!)
        let clock: @Sendable () -> Date = { Date(timeIntervalSince1970: 1000) }
        return PlaybackStartup(peer: peer, api: GatewayClient(policy: policy, transport: http, clock: clock),
                               whep: WHEPClient(policy: policy, transport: http, clock: clock))
    }
    @MainActor private func begin(_ owner: PlaybackStartup, timeout: Duration = .milliseconds(100)) async throws {
        try await owner.start(cameraID: ResourceID("cam_test"), credential: BearerCredential("SYNTHETIC_VIEWER"),
                              timeout: timeout)
    }
    @MainActor private func expectTimeout(_ owner: PlaybackStartup) async {
        do { try await begin(owner); XCTFail("Expected startup timeout") }
        catch { XCTAssertEqual(error as? PlaybackStartupError, .timedOut) }
    }

    @MainActor func testOneBudgetCoversEveryStartupPhase() async throws {
        for phase in ["offer", "grant", "post", "answer", "firstFrame"] {
            let peer = StartupPeer()
            let http = StartupHTTP()
            switch phase {
            case "offer": peer.offerDelay = .seconds(10)
            case "grant": await http.configure(grantDelay: .seconds(10))
            case "post": await http.configure(postDelay: .seconds(10))
            case "answer": peer.answerDelay = .seconds(10)
            default: peer.autoFrame = false
            }
            let owner = try startup(peer, http)
            await expectTimeout(owner)
            XCTAssertTrue(peer.closed, phase)
            let deletes = await http.deletes
            XCTAssertEqual(deletes, ["answer", "firstFrame"].contains(phase) ? 1 : 0, phase)
            let canceled = await http.canceledPhase
            if phase == "grant" || phase == "post" { XCTAssertEqual(canceled, phase) }
            if phase == "offer" || phase == "answer" { XCTAssertEqual(peer.canceledPhase, phase) }
        }
    }

    @MainActor func testBudgetDoesNotRestartAtEachPhase() async throws {
        let peer = StartupPeer(); peer.offerDelay = .milliseconds(80)
        let http = StartupHTTP(); await http.configure(grantDelay: .milliseconds(80))
        let owner = try startup(peer, http)
        do { try await begin(owner, timeout: .milliseconds(120)); XCTFail("Expected shared budget exhaustion") }
        catch { XCTAssertEqual(error as? PlaybackStartupError, .timedOut) }
        let posts = await http.posts
        XCTAssertEqual(posts, 0)
        XCTAssertTrue(peer.closed)
    }

    @MainActor func testSuccessfulStartupRetiresTimerNotTheLiveSession() async throws {
        let peer = StartupPeer(); let http = StartupHTTP(); let owner = try startup(peer, http)
        try await begin(owner, timeout: .milliseconds(200))
        XCTAssertEqual(owner.stage, .ready)
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertFalse(peer.closed, "The startup deadline must not become a viewing-time limit")
        let before = await http.deletes
        XCTAssertEqual(before, 0)
        let closed = await owner.close()
        XCTAssertTrue(closed)
        let closedAgain = await owner.close()
        XCTAssertTrue(closedAgain)
        let after = await http.deletes
        XCTAssertEqual(after, 1)
    }

    @MainActor func testCancellationDuringAnswerClosesPeerAndDeletesSession() async throws {
        let peer = StartupPeer(); peer.answerDelay = .seconds(10)
        let http = StartupHTTP(); let owner = try startup(peer, http)
        let task = Task { try await self.begin(owner, timeout: .seconds(5)) }
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while peer.answerCalls == 0, ContinuousClock.now < deadline { await Task.yield() }
        XCTAssertEqual(peer.answerCalls, 1)
        task.cancel()
        do { try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertTrue(peer.closed)
        let deletes = await http.deletes
        let canceled = await http.deleteWasCancelled
        XCTAssertEqual(deletes, 1)
        XCTAssertFalse(canceled, "Cleanup must survive cancellation of playback")
    }

    @MainActor func testPrecancelledStartupDoesNotCallPeerOrHTTP() async throws {
        let peer = StartupPeer(); let http = StartupHTTP(); let owner = try startup(peer, http)
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            try await self.begin(owner)
        }
        do { try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        XCTAssertTrue(peer.closed)
        XCTAssertEqual(peer.offerCalls, 0)
        let grants = await http.grants
        XCTAssertEqual(grants, 0)
    }

    @MainActor func testLateOfferCannotStartGrantAfterTimeout() async throws {
        let peer = StartupPeer(); peer.offerDelay = .seconds(10); peer.ignoresOfferCancellation = true
        let http = StartupHTTP(); let owner = try startup(peer, http)
        await expectTimeout(owner)
        XCTAssertTrue(peer.closedBeforeLateOffer, "Close local resources before joining the losing operation")
        let grants = await http.grants
        XCTAssertEqual(grants, 0)
    }

    @MainActor func testLateWHEPResponseStillGetsDeletedWithoutApplyingAnswer() async throws {
        let peer = StartupPeer(); let http = StartupHTTP()
        await http.configure(postDelay: .seconds(10), ignorePostCancellation: true)
        let owner = try startup(peer, http)
        await expectTimeout(owner)
        XCTAssertEqual(peer.answerCalls, 0)
        let deletes = await http.deletes
        XCTAssertEqual(deletes, 1)
        XCTAssertTrue(peer.closed)
    }

    @MainActor func testAnswerFailureRetainsOriginalErrorAndDeletes() async throws {
        let peer = StartupPeer(); peer.answerFails = true
        let http = StartupHTTP(); let owner = try startup(peer, http)
        do { try await begin(owner); XCTFail("Expected peer failure") }
        catch { XCTAssertEqual(error as? PlaybackStartupError, .peerFailed) }
        let deletes = await http.deletes
        XCTAssertEqual(deletes, 1)
        XCTAssertTrue(peer.closed)
    }

    @MainActor func testDeniedGrantDoesNotStartWHEPOrLoseAuthenticationError() async throws {
        let peer = StartupPeer(); let http = StartupHTTP(); await http.configure(grantStatus: 401)
        let owner = try startup(peer, http)
        do { try await begin(owner); XCTFail("Expected denial") }
        catch { XCTAssertEqual(error as? MonitorError, .unauthenticated) }
        let posts = await http.posts
        XCTAssertEqual(posts, 0)
        XCTAssertTrue(peer.closed)
    }

    @MainActor func testInvalidTimeoutAndOwnerReuseFailBeforeNewWork() async throws {
        for timeout in [Duration.zero, .seconds(-1), .seconds(61)] {
            let peer = StartupPeer(); let http = StartupHTTP(); let owner = try startup(peer, http)
            do { try await begin(owner, timeout: timeout); XCTFail("Expected invalid budget") }
            catch { XCTAssertEqual(error as? PlaybackStartupError, .invalidTimeout) }
            XCTAssertEqual(peer.offerCalls, 0)
            XCTAssertTrue(peer.closed)
            do { try await begin(owner); XCTFail("Closed owner must not be reused") }
            catch { XCTAssertEqual(error as? PlaybackStartupError, .alreadyUsed) }
        }
    }

    @MainActor func testFailedDeleteNeverKeepsLocalPeerAlive() async throws {
        let peer = StartupPeer(); let http = StartupHTTP(); await http.configure(deleteStatus: 503)
        let owner = try startup(peer, http)
        try await begin(owner)
        let deleted = await owner.close()
        XCTAssertFalse(deleted)
        XCTAssertTrue(peer.closed)
    }
}
