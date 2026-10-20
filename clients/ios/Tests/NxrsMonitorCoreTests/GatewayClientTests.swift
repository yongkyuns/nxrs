import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import NxrsMonitorCore

func fixture(_ name: String) throws -> Data {
    try Data(contentsOf: XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")))
}

actor StubTransport: HTTPTransport {
    let result: HTTPResult
    private(set) var requests: [URLRequest] = []
    init(_ data: Data, status: Int = 200, type: String = "application/json") {
        result = HTTPResult(data: data, response: HTTPURLResponse(url: URL(string: "https://api.example.test")!,
                            statusCode: status, httpVersion: nil, headerFields: ["Content-Type": type])!)
    }
    func send(_ request: URLRequest) async throws -> HTTPResult { requests.append(request); return result }
}

final class GatewayClientTests: XCTestCase {
    let account = try! BearerCredential("TEST_ACCOUNT_CREDENTIAL")
    let camera = try! ResourceID("cam_reference")
    func client(_ transport: any HTTPTransport, host: String = "api.example.test") throws -> GatewayClient {
        try GatewayClient(policy: EndpointPolicy(gateway: URL(string: "https://\(host)/nxrs")!,
                          additionalMediaOrigins: [URL(string: "https://media.example.test")!]), transport: transport,
                          clock: { Date(timeIntervalSince1970: 1700000000) })
    }
    func testCameraListUsesAuthenticatedVersionedContract() async throws {
        let transport = StubTransport(try fixture("cameras"))
        let cameras = try await client(transport).cameras(credential: account)
        XCTAssertEqual(cameras.count, 1)
        XCTAssertEqual(cameras[0].source, .synthetic)
        let requests = await transport.requests
        XCTAssertEqual(requests[0].url?.path, "/nxrs/v1/cameras")
        XCTAssertEqual(requests[0].value(forHTTPHeaderField: "Authorization"), "Bearer TEST_ACCOUNT_CREDENTIAL")
        XCTAssertEqual(requests[0].value(forHTTPHeaderField: "Cache-Control"), "no-store")
    }
    func testGrantIsScopedSeparateAndRedacted() async throws {
        let transport = StubTransport(try fixture("grant"), status: 201)
        let grant = try await client(transport).playbackGrant(cameraID: camera, credential: account)
        XCTAssertEqual(grant.cameraID, camera)
        XCTAssertEqual(grant.credential.value, "TEST_ONLY_NOT_A_CREDENTIAL")
        XCTAssertFalse(String(reflecting: grant).contains("TEST_ONLY"))
        let requests = await transport.requests
        XCTAssertEqual(requests[0].httpMethod, "POST")
        XCTAssertEqual(requests[0].url?.path, "/nxrs/v1/cameras/cam_reference/playback-grants")
        XCTAssertEqual(requests[0].httpBody, Data("{}".utf8))
    }
    func testSameClientWorksForVPNAndHostedAddresses() async throws {
        for host in ["10.77.0.1", "api.example.test"] {
            let transport = StubTransport(try fixture("grant"), status: 201)
            let grant = try await client(transport, host: host).playbackGrant(cameraID: camera, credential: account)
            XCTAssertEqual(grant.endpoint.host, "media.example.test")
        }
    }
    func testAuthorizationFailuresDoNotRetry() async throws {
        let cases: [(Int, MonitorError)] = [(401, .unauthenticated), (403, .forbidden), (404, .notFound),
            (409, .cameraUnavailable), (429, .rateLimited), (503, .serviceUnavailable), (302, .redirectRejected)]
        for (status, expected) in cases {
            let transport = StubTransport(Data(), status: status)
            do { _ = try await client(transport).cameras(credential: account); XCTFail("Expected failure") }
            catch { XCTAssertEqual(error as? MonitorError, expected) }
            let requests = await transport.requests
            XCTAssertEqual(requests.count, 1)
        }
    }
    func testRejectsMismatchedExpiredLongLivedAndUntrustedGrants() async throws {
        let original = try JSONSerialization.jsonObject(with: fixture("grant")) as! [String: Any]
        let cases: [(String, Any, MonitorError)] = [
            ("camera_id", "another_camera", .invalidContract), ("expires_at", 1700000000, .expiredGrant),
            ("expires_at", 1700000400, .invalidContract), ("endpoint", "https://evil.test/whep", .untrustedOrigin),
            ("api_version", "2", .invalidContract), ("protocol", "hls", .invalidContract),
            ("access_token", "token\r\nHeader: x", .invalidCredential), ("token_type", "Basic", .invalidContract)]
        for (key, value, expected) in cases {
            var body = original
            body[key] = value
            let transport = StubTransport(try JSONSerialization.data(withJSONObject: body), status: 201)
            do { _ = try await client(transport).playbackGrant(cameraID: camera, credential: account); XCTFail("Expected failure for \(key)") }
            catch { XCTAssertEqual(error as? MonitorError, expected) }
        }
    }
    func testChecksExpiryAfterTransportReturns() async throws {
        let transport = StubTransport(try fixture("grant"), status: 201)
        let policy = try EndpointPolicy(gateway: URL(string: "https://media.example.test")!)
        let client = GatewayClient(policy: policy, transport: transport,
                                   clock: { Date(timeIntervalSince1970: 1700000061) })
        do { _ = try await client.playbackGrant(cameraID: camera, credential: account); XCTFail("Expected expiry") }
        catch { XCTAssertEqual(error as? MonitorError, .expiredGrant) }
    }
    func testRejectsMalformedResponseWithoutLeakingBody() async throws {
        let transport = StubTransport(Data("SECRET_RESPONSE_BODY".utf8))
        do { _ = try await client(transport).cameras(credential: account); XCTFail("Expected failure") }
        catch {
            XCTAssertEqual(error as? MonitorError, .invalidContract)
            XCTAssertFalse(String(describing: error).contains("SECRET_RESPONSE_BODY"))
        }
    }
    func testRejectsUnexpectedContentTypeAndOversize() async throws {
        let cases: [(Data, String, MonitorError)] = [(try fixture("cameras"), "text/html", .invalidContract),
            (Data(repeating: 0, count: BoundedHTTPTransport.maximumResponseBytes + 1), "application/json", .responseTooLarge)]
        for (data, type, expected) in cases {
            let transport = StubTransport(data, type: type)
            do { _ = try await client(transport).cameras(credential: account); XCTFail("Expected failure") }
            catch { XCTAssertEqual(error as? MonitorError, expected) }
        }
    }
    func testRejectsDuplicateCameraIdentityAndMissingSource() async throws {
        let original = try JSONSerialization.jsonObject(with: fixture("cameras")) as! [String: Any]
        let cameras = original["cameras"] as! [[String: Any]]
        var duplicate = original
        duplicate["cameras"] = cameras + cameras
        var missing = original
        var camera = cameras[0]
        camera.removeValue(forKey: "source")
        missing["cameras"] = [camera]
        for body in [duplicate, missing] {
            let transport = StubTransport(try JSONSerialization.data(withJSONObject: body))
            do { _ = try await client(transport).cameras(credential: account); XCTFail("Expected failure") }
            catch { XCTAssertEqual(error as? MonitorError, .invalidContract) }
        }
    }
}
