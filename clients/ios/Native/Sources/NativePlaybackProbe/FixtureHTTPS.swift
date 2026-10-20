import Foundation
import Security
import NxrsMonitorCore

/// Test executable only: never linked into NxrsMonitorNative or the iPhone app.
/// An ephemeral CA is scoped to each URLSession, not installed in any keychain.
struct FixtureHTTPS: HTTPTransport {
    enum Failure: Error { case configuration, untrustedCertificate, regression }
    let rootDER: Data
    let origins: Set<String>

    init(rootDER: Data, endpoints: [URL]) throws {
        guard rootDER.count <= 65_536,
              SecCertificateCreateWithData(nil, rootDER as CFData) != nil else {
            throw Failure.configuration
        }
        self.rootDER = rootDER
        self.origins = try Set(endpoints.map(Self.origin))
    }

    static func origin(_ url: URL) throws -> String {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == "https", let host = parts.host,
              ["localhost", "127.0.0.1"].contains(host),
              let port = parts.port, (1...65535).contains(port),
              parts.user == nil, parts.password == nil, parts.fragment == nil else {
            throw Failure.configuration
        }
        return "\(host):\(port)"
    }

    /// Retain URLSession's SSL/hostname policies. No exceptions, hostname override,
    /// validity override or unconditional URLCredential acceptance.
    static func evaluate(_ trust: SecTrust, rootDER: Data) -> Bool {
        guard let root = SecCertificateCreateWithData(nil, rootDER as CFData),
              SecTrustSetAnchorCertificates(trust, [root] as CFArray) == errSecSuccess,
              SecTrustSetAnchorCertificatesOnly(trust, true) == errSecSuccess,
              SecTrustSetNetworkFetchAllowed(trust, false) == errSecSuccess else { return false }
        return SecTrustEvaluateWithError(trust, nil)
    }

    func send(_ request: URLRequest) async throws -> HTTPResult {
        try Task.checkCancellation()
        guard let url = request.url, origins.contains(try Self.origin(url)) else {
            throw Failure.configuration
        }
        let delegate = FixtureTrustDelegate(rootDER: rootDER, origin: try Self.origin(url))
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        config.httpShouldSetCookies = false
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 20
        let session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        do {
            return try await withTaskCancellationHandler(operation: {
                let (bytes, response) = try await session.bytes(for: request)
                guard let http = response as? HTTPURLResponse else { throw Failure.regression }
                guard !(300...399).contains(http.statusCode) else { throw MonitorError.redirectRejected }
                let limit = BoundedHTTPTransport.maximumResponseBytes
                guard response.expectedContentLength <= Int64(limit) else { throw MonitorError.responseTooLarge }
                var data = Data()
                for try await byte in bytes {
                    try Task.checkCancellation()
                    guard data.count < limit else { throw MonitorError.responseTooLarge }
                    data.append(byte)
                }
                return HTTPResult(data: data, response: http)
            }, onCancel: { session.invalidateAndCancel() })
        } catch {
            if delegate.rejectedTrust { throw Failure.untrustedCertificate }
            throw error
        }
    }
}

/// Immutable certificate/origin; the sole mutable flag is lock-protected.
private final class FixtureTrustDelegate: NSObject, URLSessionDelegate, URLSessionTaskDelegate, @unchecked Sendable {
    private let rootDER: Data
    private let origin: String
    private let lock = NSLock()
    private var rejected = false
    var rejectedTrust: Bool { lock.lock(); defer { lock.unlock() }; return rejected }
    init(rootDER: Data, origin: String) { self.rootDER = rootDER; self.origin = origin }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping @Sendable (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let space = challenge.protectionSpace
        guard space.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              !space.isProxy(), space.protocol == "https",
              "\(space.host):\(space.port)" == origin,
              let trust = space.serverTrust,
              FixtureHTTPS.evaluate(trust, rootDER: rootDER) else {
            lock.lock(); rejected = true; lock.unlock()
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

extension FixtureHTTPS {
    /// Real HTTPS negative controls plus a clock-shifted leaf-expiry evaluation.
    /// No viewer/media credential is used by any of these checks.
    func verifyBoundary(gateway: URL, wrongRootDER: Data, leafDER: Data) async throws {
        let health = URLRequest(url: gateway.appendingPathComponent("healthz"))
        try await verifySystemRejection(health)
        guard try await send(health).response.statusCode == 200 else { throw Failure.regression }
        let wrongCA = try FixtureHTTPS(rootDER: wrongRootDER, endpoints: [gateway])
        do { _ = try await wrongCA.send(health); throw Failure.regression }
        catch Failure.untrustedCertificate { }

        var ip = URLComponents(url: health.url!, resolvingAgainstBaseURL: false)!
        ip.host = "127.0.0.1" // The signed leaf has DNS:localhost only.
        let wrongHost = try FixtureHTTPS(rootDER: rootDER, endpoints: [ip.url!])
        do { _ = try await wrongHost.send(URLRequest(url: ip.url!)); throw Failure.regression }
        catch Failure.untrustedCertificate { }

        guard let leaf = SecCertificateCreateWithData(nil, leafDER as CFData) else { throw Failure.configuration }
        var trust: SecTrust?
        guard SecTrustCreateWithCertificates(leaf, SecPolicyCreateSSL(true, "localhost" as CFString), &trust) == errSecSuccess,
              let trust = trust, Self.evaluate(trust, rootDER: rootDER),
              SecTrustSetVerifyDate(trust, Date().addingTimeInterval(172_800) as CFDate) == errSecSuccess,
              !Self.evaluate(trust, rootDER: rootDER) else { throw Failure.regression }

        do { _ = try await send(URLRequest(url: gateway.appendingPathComponent("_fixture/redirect"))); throw Failure.regression }
        catch MonitorError.redirectRejected { }
        for path in ["_fixture/oversize", "_fixture/streamed-oversize"] {
            do { _ = try await send(URLRequest(url: gateway.appendingPathComponent(path))); throw Failure.regression }
            catch MonitorError.responseTooLarge { }
        }
        var otherPort = URLComponents(url: gateway, resolvingAgainstBaseURL: false)!
        otherPort.port = otherPort.port == 1 ? 2 : 1
        do { _ = try await send(URLRequest(url: otherPort.url!)); throw Failure.regression }
        catch Failure.configuration { }
        try await verifySystemRejection(health)
        print("NATIVE TLS PASS: session-scoped CA; system trust unchanged; wrong CA/hostname/expiry, redirect, origin and body bounds rejected")
    }

    private func verifySystemRejection(_ request: URLRequest) async throws {
        do { _ = try await BoundedHTTPTransport().send(request); throw Failure.regression }
        catch let error as URLError {
            guard [.serverCertificateUntrusted, .serverCertificateHasUnknownRoot,
                   .serverCertificateHasBadDate, .serverCertificateNotYetValid].contains(error.code) else { throw error }
        }
    }
}
