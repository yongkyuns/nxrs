import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import NxrsMonitorCore

private actor WHEPTransport: HTTPTransport {
    private var results: [HTTPResult]
    private(set) var requests: [URLRequest] = []
    init(_ results: [HTTPResult]) { self.results = results }
    func send(_ request: URLRequest) async throws -> HTTPResult {
        requests.append(request)
        guard !results.isEmpty else { throw URLError(.cannotConnectToHost) }
        return results.removeFirst()
    }
}

final class WHEPClientTests: XCTestCase {
    private let sdp = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
    private func grant(expiration: TimeInterval = 1700000060) throws -> PlaybackGrant {
        try PlaybackGrant(cameraID: ResourceID("cam_reference"),
            endpoint: URL(string: "https://media.test/reference/whep")!,
            credential: BearerCredential("MEDIA_ONLY"), expiresAt: Date(timeIntervalSince1970: expiration))
    }
    private func result(status: Int = 201, location: String? = "/reference/whep/session_1",
                        type: String = "application/sdp", data: Data? = nil) -> HTTPResult {
        var headers = ["Content-Type": type]
        headers["Location"] = location
        return HTTPResult(data: data ?? Data(sdp.utf8), response: HTTPURLResponse(
            url: URL(string: "https://media.test/reference/whep")!, statusCode: status,
            httpVersion: nil, headerFields: headers)!)
    }
    private func client(_ transport: any HTTPTransport) throws -> WHEPClient {
        try WHEPClient(policy: EndpointPolicy(gateway: URL(string: "https://api.test")!,
            additionalMediaOrigins: [URL(string: "https://media.test")!]), transport: transport,
            clock: { Date(timeIntervalSince1970: 1700000000) })
    }
    func testPOSTAndDELETEUseOnlyMediaCredential() async throws {
        let transport = WHEPTransport([result(), result(status: 204)])
        let client = try client(transport)
        let session = try await client.open(grant: grant(), offerSDP: sdp)
        XCTAssertEqual(session.answerSDP, sdp)
        XCTAssertFalse(String(reflecting: session).contains("MEDIA_ONLY"))
        XCTAssertFalse(String(reflecting: session).contains("session_1"))
        let deleted = await client.close(session)
        XCTAssertTrue(deleted)
        let requests = await transport.requests
        XCTAssertEqual(requests.map(\.httpMethod), ["POST", "DELETE"])
        XCTAssertEqual(requests[1].url?.path, "/reference/whep/session_1")
        XCTAssertTrue(requests.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer MEDIA_ONLY" })
        XCTAssertEqual(requests[0].httpBody, Data(sdp.utf8))
    }
    func testUntrustedLocationNeverReceivesCredentialOrDELETE() async throws {
        let transport = WHEPTransport([result(location: "https://evil.test/session")])
        do { _ = try await client(transport).open(grant: grant(), offerSDP: sdp); XCTFail("Expected rejection") }
        catch { XCTAssertEqual(error as? MonitorError, .untrustedOrigin) }
        let requests = await transport.requests
        XCTAssertEqual(requests.count, 1)
    }
    func testMalformedAnswerDeletesKnownTrustedSession() async throws {
        for response in [result(type: "text/html"), result(data: Data("invalid".utf8)),
                         result(data: Data(repeating: 0, count: WHEPClient.maximumSDPBytes + 1))] {
            let transport = WHEPTransport([response, result(status: 204)])
            do { _ = try await client(transport).open(grant: grant(), offerSDP: sdp); XCTFail("Expected rejection") }
            catch { XCTAssertEqual(error as? WHEPError, .invalidResponse) }
            let requests = await transport.requests
            XCTAssertEqual(requests.map(\.httpMethod), ["POST", "DELETE"])
        }
    }
    func testMissingLocationFailsWithoutGuessingResource() async throws {
        let transport = WHEPTransport([result(location: nil)])
        do { _ = try await client(transport).open(grant: grant(), offerSDP: sdp); XCTFail("Expected rejection") }
        catch { XCTAssertEqual(error as? WHEPError, .invalidResponse) }
        let requests = await transport.requests
        XCTAssertEqual(requests.count, 1)
    }
    func testInvalidOrExpiredInputMakesNoRequest() async throws {
        let transport = WHEPTransport([])
        do { _ = try await client(transport).open(grant: grant(expiration: 1699999999), offerSDP: sdp); XCTFail("Expected expiry") }
        catch { XCTAssertEqual(error as? MonitorError, .expiredGrant) }
        for invalid in ["", "v=0\n", "v=0\r\n\0", String(repeating: "x", count: WHEPClient.maximumSDPBytes + 1)] {
            do { _ = try await client(transport).open(grant: grant(), offerSDP: invalid); XCTFail("Expected rejection") }
            catch { XCTAssertEqual(error as? WHEPError, .invalidSDP) }
        }
        let requests = await transport.requests
        XCTAssertTrue(requests.isEmpty)
    }
    func testAuthorizationFailureIsNotRetried() async throws {
        for code in [401, 403, 302, 429] {
            let transport = WHEPTransport([result(status: code)])
            do { _ = try await client(transport).open(grant: grant(), offerSDP: sdp); XCTFail("Expected rejection") }
            catch { XCTAssertNotNil(error as? MonitorError) }
            let requests = await transport.requests
            XCTAssertEqual(requests.count, 1)
        }
    }
    func testCancelledOwnerCanStillDeleteSession() async throws {
        let transport = WHEPTransport([result(), result(status: 410)])
        let client = try client(transport)
        let session = try await client.open(grant: grant(), offerSDP: sdp)
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return await client.close(session)
        }
        let deleted = await task.value
        XCTAssertTrue(deleted)
    }
    func testCancelledBeforePOSTMakesNoRequest() async throws {
        let transport = WHEPTransport([])
        let client = try client(transport)
        let grant = try grant()
        let offer = sdp
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await client.open(grant: grant, offerSDP: offer)
        }
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
        let requests = await transport.requests
        XCTAssertTrue(requests.isEmpty)
    }
}
