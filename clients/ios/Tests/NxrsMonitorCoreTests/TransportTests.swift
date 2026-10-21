import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import NxrsMonitorCore

private final class LocalProtocol: URLProtocol {
    enum Mode { case small, declaredOversize, streamedOversize, pending }
    private static let lock = NSLock()
    private static var mode: Mode = .small
    static func set(_ value: Mode) { lock.lock(); mode = value; lock.unlock() }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        let mode = Self.mode
        Self.lock.unlock()
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
            } catch { XCTAssertEqual(error as? MonitorError, .responseTooLarge) }
        }
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
