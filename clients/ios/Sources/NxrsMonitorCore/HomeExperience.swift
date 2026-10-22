import Foundation

/// Local presentation preferences never change a camera's server identity or permissions.
public struct CameraPersonalization: Codable, Equatable, Sendable {
    public var name: String
    public var room: String
    public var favorite: Bool

    public init(name: String = "", room: String = "", favorite: Bool = false) {
        self.name = String(name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(48))
        self.room = String(room.trimmingCharacters(in: .whitespacesAndNewlines).prefix(32))
        self.favorite = favorite
    }
}

public enum HomeAppearance: String, Codable, CaseIterable, Sendable {
    case system, light, dark
}

public struct HomePreferences: Codable, Equatable, Sendable {
    public var homeName = "My home"
    public var appearance: HomeAppearance = .system
    public var autoConnect = true
    public var keepScreenAwake = true
    public var lastCameraID: String?
    public var cameras: [String: CameraPersonalization] = [:]
    public init() {}

    public func personalization(for camera: CameraSummary) -> CameraPersonalization {
        cameras[camera.cameraID.value] ?? CameraPersonalization()
    }

    public func displayName(for camera: CameraSummary) -> String {
        let custom = personalization(for: camera).name
        return custom.isEmpty ? camera.name : custom
    }
}

/// Explicit import format. Credentials are validated before a connection is attempted.
/// The invite is deliberately not printable and must never be written to preferences.
public struct EnrollmentInvitation: Sendable, CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public let gateway: URL
    public let mediaOrigin: URL?
    public let credential: BearerCredential
    public let homeName: String
    public var description: String { "<private home invitation>" }
    public var debugDescription: String { description }
    public var customMirror: Mirror { Mirror(self, children: [:]) }

    public init(data: Data) throws {
        guard data.count <= 16_384 else { throw MonitorError.responseTooLarge }
        struct Payload: Decodable {
            let version: Int
            let gateway: URL
            let mediaOrigin: URL?
            let viewerCredential: String
            let homeName: String?
        }
        let payload = try JSONDecoder().decode(Payload.self, from: data)
        guard payload.version == 1 else { throw MonitorError.invalidContract }
        _ = try EndpointPolicy(gateway: payload.gateway,
                               additionalMediaOrigins: payload.mediaOrigin.map { [$0] } ?? [])
        credential = try BearerCredential(payload.viewerCredential)
        gateway = payload.gateway
        mediaOrigin = payload.mediaOrigin
        homeName = String((payload.homeName ?? "My home")
            .trimmingCharacters(in: .whitespacesAndNewlines).prefix(48))
    }

    /// Only the enrollment owner uses this; descriptions always remain redacted.
    public func withCredential<T>(_ body: (String) throws -> T) rethrows -> T {
        try body(credential.value)
    }
}

public enum SavedCaptureKind: String, Codable, CaseIterable, Sendable {
    case photo, clip
    public var fileExtension: String { self == .photo ? "jpg" : "mov" }
}

public struct SavedCapture: Codable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public let cameraID: String
    public let cameraName: String
    public let createdAt: Date
    public let kind: SavedCaptureKind
    public let duration: TimeInterval
    public let byteCount: Int
    public var favorite: Bool
    public var filename: String { id.uuidString + "." + kind.fileExtension }
}

public enum CaptureLibraryError: Error, Equatable, Sendable {
    case full, invalidMedia, invalidStagingFile, missingCapture, corruptIndex
}
