import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public struct ActivityEvent: Codable, Equatable, Identifiable, Sendable {
    public let eventID: String
    public let cameraID: ResourceID
    public let kind: String
    public let startedAtMs: UInt64
    public let endedAtMs: UInt64
    public let clipBytes: UInt64
    public let thumbnailBytes: UInt64
    public let kept: Bool
    public let interrupted: Bool
    public var id: String { eventID }
    public var startedAt: Date { Date(timeIntervalSince1970: Double(startedAtMs) / 1000) }
    public var duration: TimeInterval { Double(endedAtMs - min(startedAtMs, endedAtMs)) / 1000 }
    enum CodingKeys: String, CodingKey {
        case eventID = "event_id", cameraID = "camera_id", kind, startedAtMs = "started_at_ms"
        case endedAtMs = "ended_at_ms", clipBytes = "clip_bytes", thumbnailBytes = "thumbnail_bytes", kept, interrupted
    }
    func validate(camera: ResourceID) throws {
        guard validEventID(eventID), cameraID == camera, kind == "motion", startedAtMs > 0,
              endedAtMs > startedAtMs, endedAtMs - startedAtMs <= 120_000,
              (1...33_554_432).contains(clipBytes), (1...262_144).contains(thumbnailBytes)
        else { throw MonitorError.invalidContract }
    }
}
func validEventID(_ value: String) -> Bool {
    value.utf8.count == 32 && value.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
}
public struct ActivityHealth: Decodable, Equatable, Sendable {
    public let updatedAtMs: UInt64
    public let lastFrameAtMs: UInt64?
    public let state: String
    public let reconnects: UInt64
    public let framesObserved: UInt64
    public let clipsCreated: UInt64
    public let clipsDropped: UInt64
    enum CodingKeys: String, CodingKey {
        case updatedAtMs = "updated_at_ms", lastFrameAtMs = "last_frame_at_ms", state, reconnects
        case framesObserved = "frames_observed", clipsCreated = "clips_created", clipsDropped = "clips_dropped"
    }
}
public struct ActivityPage: Decodable, Sendable {
    public let apiVersion: String
    public let cameraID: ResourceID
    public let events: [ActivityEvent]
    public let nextCursor: String?
    public let canManage: Bool
    public let usedBytes: UInt64
    public let maxBytes: UInt64
    public let retentionSeconds: UInt64
    public let health: ActivityHealth?
    public let observedAtMs: UInt64
    enum CodingKeys: String, CodingKey {
        case apiVersion = "api_version", cameraID = "camera_id", events, nextCursor = "next_cursor"
        case canManage = "can_manage", usedBytes = "used_bytes", maxBytes = "max_bytes"
        case retentionSeconds = "retention_seconds", health, observedAtMs = "observed_at_ms"
    }
    public var monitoring: Bool {
        guard let health, health.state == "monitoring", let last = health.lastFrameAtMs else { return false }
        return observedAtMs >= health.updatedAtMs && observedAtMs - health.updatedAtMs <= 10_000
            && observedAtMs >= last && observedAtMs - last <= 10_000
    }
    func validate(camera: ResourceID) throws {
        guard apiVersion == "1", cameraID == camera, events.count <= 50,
              Set(events.map(\.eventID)).count == events.count,
              nextCursor == nil || validEventID(nextCursor!), usedBytes <= maxBytes,
              maxBytes <= 107_374_182_400, (60...2_592_000).contains(retentionSeconds), observedAtMs > 0,
              health.map({ ["connecting", "monitoring", "reconnecting", "storage_full", "recording_error", "stopped"].contains($0.state) }) ?? true
        else { throw MonitorError.invalidContract }
        for event in events { try event.validate(camera: camera) }
    }
}

public struct ActivityClient: Sendable {
    private let gateway: GatewayClient
    private let transport: any HTTPTransport
    private let media: any HTTPTransport
    public init(policy: EndpointPolicy, transport: any HTTPTransport = BoundedHTTPTransport(),
                mediaTransport: any HTTPTransport = BoundedHTTPTransport()) {
        gateway = GatewayClient(policy: policy, transport: transport); self.transport = transport; media = mediaTransport
    }
    public func page(camera: ResourceID, before: String? = nil, keptOnly: Bool = false, credential: BearerCredential) async throws -> ActivityPage {
        var path = ["v1", "cameras", camera.value, "activity"]
        if keptOnly { path.append("kept") }
        if let before { guard validEventID(before) else { throw MonitorError.invalidIdentifier }; path += ["before", before] }
        let result = try await transport.send(gateway.request(path: path, method: "GET", credential: credential))
        let page: ActivityPage = try gateway.decode(result, status: 200)
        try page.validate(camera: camera)
        guard !keptOnly || page.events.allSatisfy(\.kept) else { throw MonitorError.invalidContract }
        return page
    }
    public func media(event: ActivityEvent, thumbnail: Bool, credential: BearerCredential) async throws -> Data {
        try event.validate(camera: event.cameraID)
        return try await withThrowingTaskGroup(of: Data.self) { group in
            group.addTask { try await download(event: event, thumbnail: thumbnail, credential: credential) }
            group.addTask { try await Task.sleep(nanoseconds: 90_000_000_000); throw URLError(.timedOut) }
            defer { group.cancelAll() }
            guard let data = try await group.next() else { throw MonitorError.invalidContract }
            return data
        }
    }
    private func download(event: ActivityEvent, thumbnail: Bool, credential: BearerCredential) async throws -> Data {
        let type = thumbnail ? "image/jpeg" : "application/octet-stream"
        let length = thumbnail ? event.thumbnailBytes : event.clipBytes
        let pieces = thumbnail ? 1 : Int((length + 524_287) / 524_288)
        var bytes = Data(); bytes.reserveCapacity(Int(length))
        for part in 0..<pieces {
            try Task.checkCancellation()
            var path = ["v1", "cameras", event.cameraID.value, "activity", event.eventID]
            path += thumbnail ? ["thumbnail"] : ["clip", "parts", String(part)]
            var request = gateway.request(path: path, method: "GET", credential: credential)
            request.setValue(type, forHTTPHeaderField: "Accept")
            var result = try await media.send(request)
            for attempt in 0..<2 where result.response.statusCode == 429 {
                try await Task.sleep(nanoseconds: UInt64(attempt + 1) * 150_000_000)
                try Task.checkCancellation()
                result = try await media.send(request)
            }
            if result.response.statusCode != 200 {
                let _: ActivityPage = try gateway.decode(result, status: 200); throw MonitorError.invalidContract
            }
            let expected = thumbnail ? length : min(length - UInt64(bytes.count), 524_288)
            guard UInt64(result.data.count) == expected,
                  result.response.value(forHTTPHeaderField: "Content-Type")?.split(separator: ";").first == Substring(type)
            else { throw MonitorError.invalidContract }
            bytes.append(result.data)
        }
        try Task.checkCancellation(); return bytes
    }
    public func keep(event: ActivityEvent, kept: Bool, credential: BearerCredential) async throws -> ActivityEvent {
        try event.validate(camera: event.cameraID)
        var request = gateway.request(path: ["v1", "cameras", event.cameraID.value, "activity", event.eventID, "keep"],
                                      method: "POST", credential: credential)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(Keep(kept: kept))
        let result = try await transport.send(request)
        let response: Updated = try gateway.decode(result, status: 200)
        try response.event.validate(camera: event.cameraID)
        guard response.apiVersion == "1", response.event.eventID == event.eventID, response.event.kept == kept else { throw MonitorError.invalidContract }
        return response.event
    }
    public func remove(event: ActivityEvent, credential: BearerCredential) async throws {
        try event.validate(camera: event.cameraID)
        var request = gateway.request(path: ["v1", "cameras", event.cameraID.value, "activity", event.eventID, "delete"],
                                      method: "POST", credential: credential)
        request.httpBody = Data("{}".utf8)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let result = try await transport.send(request)
        if result.response.statusCode != 204 {
            let _: ActivityPage = try gateway.decode(result, status: 204)
            throw MonitorError.invalidContract
        }
        guard result.data.isEmpty else { throw MonitorError.invalidContract }
    }
    private struct Keep: Encodable { let kept: Bool }
    private struct Updated: Decodable {
        let apiVersion: String; let event: ActivityEvent
        enum CodingKeys: String, CodingKey { case apiVersion = "api_version", event }
    }
}
