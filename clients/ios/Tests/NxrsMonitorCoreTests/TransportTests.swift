import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import NxrsMonitorCore

private final class LocalProtocol: URLProtocol {
    enum Mode { case small, declaredOversize, streamedOversize, pending, createdFailure, createdOversize, createdUntrusted, failedNonCreation }
    private static let lock = NSLock()
    private static var mode: Mode = .small
    private static var methods: [String] = []
    static func set(_ value: Mode) { lock.lock(); mode = value; methods = []; lock.unlock() }
    static func requestedMethods() -> [String] { lock.lock(); defer { lock.unlock() }; return methods }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        let mode = Self.mode
        Self.methods.append(request.httpMethod ?? "GET")
        Self.lock.unlock()
        if [.createdFailure, .createdOversize, .createdUntrusted, .failedNonCreation].contains(mode) {
            let deletion = request.httpMethod == "DELETE"
            var headers = ["Location": mode == .createdUntrusted ? "https://evil.test/session_1" : "/reference/whep/session_1",
                           "Content-Type":"application/sdp"]
            if mode == .createdOversize && !deletion { headers["Content-Length"] = "\(BoundedHTTPTransport.maximumResponseBytes + 1)" }
            let response = HTTPURLResponse(url: request.url!, statusCode: deletion ? 204 : (mode == .failedNonCreation ? 503 : 201),
                httpVersion: "HTTP/1.1", headerFields: headers)!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            if deletion || mode == .createdOversize { client?.urlProtocolDidFinishLoading(self) }
            else { client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost)) }
            return
        }
        if case .pending = mode { return }
        let length = mode == .declaredOversize ? BoundedHTTPTransport.maximumResponseBytes + 1 : 2
        let headers = mode == .streamedOversize ? [:] : ["Content-Length": "\(length)"]
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1", headerFields: headers)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if mode == .streamedOversize {
            // No Content-Length: the delegate must enforce the cap during accumulation.
            client?.urlProtocol(self, didLoad: Data(repeating: 0, count: BoundedHTTPTransport.maximumResponseBytes))
            client?.urlProtocol(self, didLoad: Data([0]))
        } else if mode == .small {
            client?.urlProtocol(self, didLoad: Data("{}".utf8))
        }
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

final class TransportTests: XCTestCase {
    func testFailedCreatedResponseDeletesKnownTrustedWHEPResource() async throws {
        let transport = BoundedHTTPTransport(protocols: [LocalProtocol.self])
        let endpoint = URL(string: "https://fixture.test/reference/whep")!
        let client = try WHEPClient(policy: EndpointPolicy(gateway: URL(string: "https://api.test")!,
            additionalMediaOrigins: [URL(string: "https://fixture.test")!]), transport: transport,
            clock: { Date(timeIntervalSince1970: 1700000000) })
        let grant = try PlaybackGrant(cameraID: ResourceID("cam_reference"), endpoint: endpoint,
            credential: BearerCredential("MEDIA_ONLY"), expiresAt: Date(timeIntervalSince1970: 1700000060))
        for mode in [LocalProtocol.Mode.createdFailure, .createdOversize, .createdUntrusted, .failedNonCreation] {
            LocalProtocol.set(mode)
            do {
                _ = try await client.open(grant: grant, offerSDP: "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n")
                XCTFail("Expected interrupted or oversized body")
            } catch {
                if mode == .createdOversize { XCTAssertEqual(error as? MonitorError, .responseTooLarge) }
                else if mode == .createdUntrusted { XCTAssertEqual(error as? MonitorError, .untrustedOrigin) }
                else { XCTAssertEqual((error as? URLError)?.code, .networkConnectionLost) }
            }
            let deletes = mode == .createdFailure || mode == .createdOversize
            XCTAssertEqual(LocalProtocol.requestedMethods(), deletes ? ["POST", "DELETE"] : ["POST"])
        }
    }
    func testFailedTransferRetainsHeadersAndRedactsResourceLocation() async throws {
        LocalProtocol.set(.createdFailure)
        do {
            _ = try await BoundedHTTPTransport(protocols: [LocalProtocol.self]).send(URLRequest(url: URL(string:"https://fixture.test")!))
            XCTFail("Expected body failure")
        } catch let failure as HTTPTransferFailure {
            XCTAssertEqual(failure.response.statusCode, 201)
            XCTAssertEqual(failure.response.value(forHTTPHeaderField: "Location"), "/reference/whep/session_1")
            XCTAssertEqual((failure.cause as? URLError)?.code, .networkConnectionLost)
            XCTAssertFalse(String(reflecting: failure).contains("session_1"))
            XCTAssertFalse(String(describing: failure).contains("fixture.test"))
        }
    }
    func testRealDelegateReceivesBoundedResponse() async throws {
        LocalProtocol.set(.small)
        let transport = BoundedHTTPTransport(protocols: [LocalProtocol.self])
        let response = try await transport.send(URLRequest(url: URL(string: "https://fixture.test")!))
        XCTAssertEqual(response.data, Data("{}".utf8))
    }
    func testRejectsDeclaredAndStreamedOversizeResponses() async throws {
        for mode in [LocalProtocol.Mode.declaredOversize, .streamedOversize] {
            LocalProtocol.set(mode)
            let transport = BoundedHTTPTransport(protocols: [LocalProtocol.self])
            do {
                _ = try await transport.send(URLRequest(url: URL(string: "https://fixture.test")!))
                XCTFail("Expected bound enforcement")
            } catch let failure as HTTPTransferFailure {
                XCTAssertEqual(failure.cause as? MonitorError, .responseTooLarge)
                XCTAssertEqual(failure.response.statusCode, 200)
            }
        }
    }
    func testGatewayPreservesUnderlyingTransportFailure() async throws {
        LocalProtocol.set(.declaredOversize)
        let api = try GatewayClient(policy: EndpointPolicy(gateway: URL(string:"https://fixture.test")!),
            transport: BoundedHTTPTransport(protocols: [LocalProtocol.self]))
        do { _ = try await api.cameras(credential: BearerCredential("VIEWER_ONLY")); XCTFail("Expected size refusal") }
        catch { XCTAssertEqual(error as? MonitorError, .responseTooLarge) }
    }
    func testCancellationRetiresPendingTransfer() async throws {
        LocalProtocol.set(.pending)
        let transport = BoundedHTTPTransport(protocols: [LocalProtocol.self])
        let task = Task { try await transport.send(URLRequest(url: URL(string: "https://fixture.test")!)) }
        try await Task.sleep(nanoseconds: 10_000_000)
        task.cancel()
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }
    func testCancellationBeforeStartIsSafe() async throws {
        LocalProtocol.set(.pending)
        let transport = BoundedHTTPTransport(protocols: [LocalProtocol.self])
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await transport.send(URLRequest(url: URL(string: "https://fixture.test")!))
        }
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }
    func testHTTPIsRejectedBeforeNetworkAccess() async throws {
        do {
            _ = try await BoundedHTTPTransport().send(URLRequest(url: URL(string: "http://fixture.test")!))
            XCTFail("Expected insecure transport rejection")
        } catch { XCTAssertEqual(error as? MonitorError, .invalidEndpoint) }
    }
}
