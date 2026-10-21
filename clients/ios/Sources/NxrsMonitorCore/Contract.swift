import Foundation

public enum MonitorError: Error, Equatable, Sendable {
    case invalidIdentifier, invalidCredential, invalidEndpoint, untrustedOrigin
    case invalidContract, expiredGrant, redirectRejected, responseTooLarge
    case unauthenticated, forbidden, notFound, cameraUnavailable, rateLimited
    case serviceUnavailable, unexpectedStatus(Int), invalidTime
}

/// Opaque server identity. Never an IP address, path, or WireGuard peer key.
public struct ResourceID: Codable, Hashable, Sendable {
    public let value: String
    public init(_ value: String) throws {
        guard (1...64).contains(value.utf8.count), value.utf8.allSatisfy({
            (65...90).contains($0) || (97...122).contains($0) ||
            (48...57).contains($0) || $0 == 45 || $0 == 95
        }) else { throw MonitorError.invalidIdentifier }
        self.value = value
    }
    public init(from decoder: Decoder) throws {
        try self.init(decoder.singleValueContainer().decode(String.self))
    }
    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(value)
    }
}

/// Intentionally not Codable or printable: credentials must not enter diagnostics.
public struct BearerCredential: Sendable, CustomStringConvertible, CustomDebugStringConvertible,
                                CustomReflectable {
    let value: String
    public init(_ value: String) throws {
        let bytes = Array(value.utf8)
        let body = bytes.prefix { $0 != 61 }
        guard (1...8192).contains(bytes.count), !body.isEmpty,
              body.allSatisfy({ (65...90).contains($0) || (97...122).contains($0) ||
                  (48...57).contains($0) || [45, 46, 95, 126, 43, 47].contains($0) }),
              bytes.dropFirst(body.count).allSatisfy({ $0 == 61 }) else {
            throw MonitorError.invalidCredential
        }
        self.value = value
    }
    public var description: String { "<redacted credential>" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: [:]) }
}

public struct CameraSummary: Decodable, Equatable, Sendable {
    public enum Source: String, Decodable, Sendable { case synthetic, physical }
    public enum Availability: String, Decodable, Sendable { case unknown, online, offline }
    public let cameraID: ResourceID
    public let gatewayID: ResourceID
    public let siteID: ResourceID
    public let name: String
    public let source: Source
    /// Gateway observation only; this is NOT evidence of decoded/fresh video.
    public let availability: Availability
    public let liveVideo: Bool
    enum CodingKeys: String, CodingKey {
        case cameraID = "camera_id", gatewayID = "gateway_id", siteID = "site_id"
        case name, source, availability, liveVideo = "live_video"
    }
}

struct CameraList: Decodable {
    let apiVersion: String
    let cameras: [CameraSummary]
    enum CodingKeys: String, CodingKey { case apiVersion = "api_version", cameras }
}

/// A media-only credential. The account credential is never reused for WHEP.
public struct PlaybackGrant: Sendable, CustomStringConvertible, CustomDebugStringConvertible,
                             CustomReflectable {
    public let cameraID: ResourceID
    public let endpoint: URL
    public let credential: BearerCredential
    public let expiresAt: Date
    public var description: String { "PlaybackGrant(camera: \(cameraID.value), credential: <redacted>)" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: ["cameraID": cameraID.value]) }
}

struct GrantResponse: Decodable {
    let apiVersion: String
    let cameraID: ResourceID
    let protocolName: String
    let endpoint: URL
    let accessToken: String
    let tokenType: String
    let expiresAt: Int64
    enum CodingKeys: String, CodingKey {
        case apiVersion = "api_version", cameraID = "camera_id", protocolName = "protocol"
        case endpoint, accessToken = "access_token", tokenType = "token_type", expiresAt = "expires_at"
    }
}
