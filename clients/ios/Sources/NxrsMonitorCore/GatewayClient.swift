import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public struct HTTPResult: Sendable {
    public let data: Data
    public let response: HTTPURLResponse
    public init(data: Data, response: HTTPURLResponse) { self.data = data; self.response = response }
}

public protocol HTTPTransport: Sendable {
    func send(_ request: URLRequest) async throws -> HTTPResult
}

public struct GatewayClient: Sendable {
    public let policy: EndpointPolicy
    private let transport: any HTTPTransport
    private let clock: @Sendable () -> Date
    public init(policy: EndpointPolicy, transport: any HTTPTransport = BoundedHTTPTransport(),
                clock: @escaping @Sendable () -> Date = { Date() }) {
        self.policy = policy
        self.transport = transport
        self.clock = clock
    }

    public func cameras(credential: BearerCredential) async throws -> [CameraSummary] {
        let request = request(path: ["v1", "cameras"], method: "GET", credential: credential)
        let result = try await transport.send(request)
        let list: CameraList = try decode(result, status: 200)
        guard list.apiVersion == "1", list.cameras.count <= 64,
              Set(list.cameras.map(\.cameraID)).count == list.cameras.count,
              list.cameras.allSatisfy({ !$0.name.isEmpty && $0.name.unicodeScalars.count <= 128 })
        else { throw MonitorError.invalidContract }
        return list.cameras
    }

    public func playbackGrant(cameraID: ResourceID, credential: BearerCredential) async throws -> PlaybackGrant {
        var request = request(path: ["v1", "cameras", cameraID.value, "playback-grants"],
                              method: "POST", credential: credential)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)
        let result = try await transport.send(request)
        let grant: GrantResponse = try decode(result, status: 201)
        guard grant.apiVersion == "1", grant.protocolName == "whep", grant.tokenType == "Bearer",
              grant.cameraID == cameraID else { throw MonitorError.invalidContract }
        let expiration = Date(timeIntervalSince1970: TimeInterval(grant.expiresAt))
        let lifetime = expiration.timeIntervalSince(clock())
        guard lifetime > 0 else { throw MonitorError.expiredGrant }
        // The server contract caps grants at 300 s; allow 30 s clock skew in this upper check.
        guard lifetime <= 330 else { throw MonitorError.invalidContract }
        return try PlaybackGrant(cameraID: cameraID, endpoint: policy.validateMediaEndpoint(grant.endpoint),
                                 credential: BearerCredential(grant.accessToken), expiresAt: expiration)
    }

    private func request(path: [String], method: String, credential: BearerCredential) -> URLRequest {
        var request = URLRequest(url: policy.apiURL(path))
        request.httpMethod = method
        request.timeoutInterval = 15
        request.setValue("Bearer \(credential.value)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        return request
    }

    private func decode<T: Decodable>(_ result: HTTPResult, status: Int) throws -> T {
        switch result.response.statusCode {
        case status: break
        case 300...399: throw MonitorError.redirectRejected
        case 401: throw MonitorError.unauthenticated
        case 403: throw MonitorError.forbidden
        case 404: throw MonitorError.notFound
        case 409: throw MonitorError.cameraUnavailable
        case 429: throw MonitorError.rateLimited
        case 500...599: throw MonitorError.serviceUnavailable
        default: throw MonitorError.unexpectedStatus(result.response.statusCode)
        }
        guard result.data.count <= BoundedHTTPTransport.maximumResponseBytes else {
            throw MonitorError.responseTooLarge
        }
        let type = result.response.value(forHTTPHeaderField: "Content-Type")?
            .split(separator: ";").first?.trimmingCharacters(in: .whitespaces).lowercased()
        guard type == "application/json" else { throw MonitorError.invalidContract }
        do { return try JSONDecoder().decode(T.self, from: result.data) }
        catch { throw MonitorError.invalidContract } // Never echo a response containing credentials.
    }
}
