import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum WHEPError: Error, Equatable, Sendable {
    case invalidSDP, invalidResponse, unavailable, rejected(Int)
}

/// Owns a server resource, not a peer connection. Treat its Location as a capability.
/// Never persist or print SDP, session URLs, or media credentials.
public struct WHEPSession: Sendable, CustomStringConvertible, CustomDebugStringConvertible,
                           CustomReflectable {
    public let answerSDP: String
    fileprivate let url: URL
    fileprivate let credential: BearerCredential
    public var description: String { "<WHEP session: redacted>" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: [:]) }
}

/// Initial reachable-LAN/VPN profile uses complete ICE gathering before POST.
/// No trickle PATCH, ICE restart or TURN discovery is silently assumed. Reconnect
/// creates a new peer/grant/session; those extensions remain inside this adapter.
public struct WHEPClient: Sendable {
    public static let maximumSDPBytes = 262_144
    private let policy: EndpointPolicy
    private let transport: any HTTPTransport
    private let clock: @Sendable () -> Date

    public init(policy: EndpointPolicy, transport: any HTTPTransport = BoundedHTTPTransport(),
                clock: @escaping @Sendable () -> Date = { Date() }) {
        self.policy = policy
        self.transport = transport
        self.clock = clock
    }

    public func open(grant: PlaybackGrant, offerSDP: String) async throws -> WHEPSession {
        try Task.checkCancellation()
        guard grant.expiresAt > clock() else { throw MonitorError.expiredGrant }
        let endpoint = try policy.validateMediaEndpoint(grant.endpoint)
        guard Self.validSDP(offerSDP) else { throw WHEPError.invalidSDP }
        var request = authorizedRequest(endpoint, method: "POST", credential: grant.credential)
        request.setValue("application/sdp", forHTTPHeaderField: "Content-Type")
        request.setValue("application/sdp", forHTTPHeaderField: "Accept")
        request.httpBody = Data(offerSDP.utf8)
        let result: HTTPResult
        do { result = try await transport.send(request) }
        catch let failure as HTTPTransferFailure {
            if failure.response.statusCode == 201,
               failure.response.value(forHTTPHeaderField: "Location") != nil {
                let provisional = try allocated(failure.response, endpoint: endpoint, credential: grant.credential)
                _ = await close(provisional)
            }
            throw failure.cause
        }
        try Self.checkStatus(result.response.statusCode, expected: 201)
        let provisional = try allocated(result.response, endpoint: endpoint, credential: grant.credential)
        do {
            try Task.checkCancellation()
            let contentType = result.response.value(forHTTPHeaderField: "Content-Type")?
                .split(separator: ";").first?.trimmingCharacters(in: .whitespaces).lowercased()
            guard contentType == "application/sdp", result.data.count <= Self.maximumSDPBytes,
                  let answer = String(data: result.data, encoding: .utf8), Self.validSDP(answer)
            else { throw WHEPError.invalidResponse }
            return WHEPSession(answerSDP: answer, url: provisional.url, credential: grant.credential)
        } catch {
            _ = await close(provisional)
            throw error
        }
    }

    private func allocated(_ response: HTTPURLResponse, endpoint: URL, credential: BearerCredential) throws -> WHEPSession {
        guard let location = response.value(forHTTPHeaderField: "Location") else {
            // No safe way to identify/delete a resource without its Location.
            throw WHEPError.invalidResponse
        }
        return WHEPSession(answerSDP: "", url: try policy.sessionURL(location: location, endpoint: endpoint),
                           credential: credential)
    }

    /// Best-effort bounded cleanup also runs when the caller has been cancelled.
    /// The owner closes the local peer immediately; this result reports only DELETE.
    @discardableResult public func close(_ session: WHEPSession) async -> Bool {
        let transport = transport
        let request = authorizedRequest(session.url, method: "DELETE", credential: session.credential)
        return await Task.detached(priority: .utility) {
            do {
                let result = try await transport.send(request)
                return [200, 204, 404, 410].contains(result.response.statusCode)
            } catch { return false }
        }.value
    }

    private func authorizedRequest(_ url: URL, method: String, credential: BearerCredential) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = 15
        request.setValue("Bearer \(credential.value)", forHTTPHeaderField: "Authorization")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        return request
    }

    private static func validSDP(_ text: String) -> Bool {
        !text.isEmpty && text.utf8.count <= maximumSDPBytes && text.hasPrefix("v=0\r\n")
            && !text.utf8.contains(0)
    }

    private static func checkStatus(_ status: Int, expected: Int) throws {
        switch status {
        case expected: return
        case 300...399: throw MonitorError.redirectRejected
        case 401: throw MonitorError.unauthenticated
        case 403: throw MonitorError.forbidden
        case 404, 409, 503: throw WHEPError.unavailable
        case 429: throw MonitorError.rateLimited
        case 500...599: throw MonitorError.serviceUnavailable
        default: throw WHEPError.rejected(status)
        }
    }
}
