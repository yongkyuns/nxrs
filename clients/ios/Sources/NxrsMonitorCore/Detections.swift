import Foundation

public enum DetectionPresence: String, Decodable, Sendable { case unavailable, clear, person }
public struct DetectionBounds: Decodable, Sendable, Equatable {
    public let left, top, right, bottom: Int
}
public struct DetectedObject: Decodable, Sendable {
    public let `class`: String
    public let score_permille: Int
    public let bounds: DetectionBounds
}
public struct DetectionObservation: Decodable, Sendable {
    public let frame_sequence, captured_ms, inference_us: UInt64
    public let person_score_permille: Int
    public let objects: [DetectedObject]
    public let truncated: Bool
}
public struct DetectionEvent: Decodable, Sendable {
    public let id, at_ms: UInt64
    public let kind: String
}
public struct DetectionSource: Decodable, Sendable {
    public let api_version: String
    public let publication_id, emitted_ms, event_count: UInt64
    public let presence: DetectionPresence
    public let localization: Bool
    public let latest: DetectionObservation?
    public let events: [DetectionEvent]
    public func validate() throws {
        guard self.api_version == "1", self.publication_id > 0, self.events.count <= 8,
              self.latest != nil || self.presence == .unavailable else { throw MonitorError.invalidContract }
        var previous: UInt64 = 0
        for event in self.events {
            guard event.id > previous, event.id <= self.event_count, event.at_ms <= self.emitted_ms,
                  ["person_entered", "person_left", "unavailable"].contains(event.kind) else { throw MonitorError.invalidContract }
            previous = event.id
        }
        guard previous == self.event_count else { throw MonitorError.invalidContract }
        if let o = self.latest {
            guard o.frame_sequence > 0, o.captured_ms <= self.emitted_ms,
                  (0...1000).contains(o.person_score_permille), o.objects.count <= 10,
                  self.localization || (o.objects.isEmpty && !o.truncated) else { throw MonitorError.invalidContract }
            for object in o.objects {
                let b = object.bounds
                guard object.class == "person", (0...1000).contains(object.score_permille),
                      0 <= b.left, b.left < b.right, b.right <= 1000,
                      0 <= b.top, b.top < b.bottom, b.bottom <= 1000 else { throw MonitorError.invalidContract }
            }
        }
    }

}
public struct DetectionSnapshot: Decodable, Sendable {
    public let api_version, camera_id: String
    public let source_session_id: String?
    public let revision, stream_elapsed_ms: UInt64
    public let source_online: Bool
    public let presence: DetectionPresence
    public let observation_age_ms: UInt64?
    public let valid_for_ms: UInt64
    public let frame_synchronized: Bool
    public let source: DetectionSource?
    public let event_gap: Bool

    public func validate(cameraID: ResourceID) throws {
        guard api_version == "1", camera_id == cameraID.value, (1...1500).contains(valid_for_ms),
              source_session_id.map({ $0.utf8.count == 32 && $0.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) } }) ?? (source == nil),
              source == nil || source_session_id != nil else { throw MonitorError.invalidContract }
        if let s = source {
            try s.validate()
        }
        if presence != .unavailable {
            guard source_online, presence == source?.presence, source?.latest != nil,
                  observation_age_ms.map({ $0 < 5000 }) == true else { throw MonitorError.invalidContract }
        }
    }
}
public struct DetectionMessage: Sendable {
    public let snapshot: DetectionSnapshot
    public let cursor: String
    public let receivedAt, requestStartedAt: TimeInterval
}

/// Presentation expiry uses monotonic clocks. A delayed SSE event cannot renew
/// itself simply by arriving later: request elapsed minus server stream elapsed
/// conservatively includes setup and transport delay, without UTC clock sync.
public struct DetectionState: Sendable {
    public private(set) var snapshot: DetectionSnapshot?
    public private(set) var eventGap = false
    private var deadline: TimeInterval = 0
    private var receipt: TimeInterval = 0
    private var age: TimeInterval = 0
    public init() {}
    public mutating func reset() { self = DetectionState() }
    public mutating func receive(_ message: DetectionMessage, cameraID: ResourceID) throws {
        let s = message.snapshot
        try s.validate(cameraID: cameraID)
        guard message.receivedAt.isFinite, message.requestStartedAt.isFinite,
              message.receivedAt >= message.requestStartedAt else { throw MonitorError.invalidTime }
        let elapsed = (message.receivedAt - message.requestStartedAt) * 1000
        guard Double(s.stream_elapsed_ms) <= elapsed + 100 else { throw MonitorError.invalidContract }
        let lag = max(0, elapsed - Double(s.stream_elapsed_ms))
        let prior = snapshot
        if prior?.source_session_id == s.source_session_id, let prior = prior, s.revision < prior.revision {
            throw MonitorError.invalidContract
        }
        eventGap = s.event_gap
        if let prior = prior, prior.source_session_id == s.source_session_id,
           let count = prior.source?.event_count, let first = s.source?.events.first?.id {
            eventGap = eventGap || (first > count && first - count > 1)
        }
        snapshot = s; receipt = message.receivedAt
        deadline = receipt + max(0, Double(s.valid_for_ms) - lag) / 1000
        age = Double(s.observation_age_ms ?? 5000) / 1000 + lag / 1000
    }
    public func presence(now: TimeInterval, videoLive: Bool) -> DetectionPresence {
        guard videoLive, now.isFinite, now >= receipt, now < deadline,
              age + now - receipt < 5, let s = snapshot else { return .unavailable }
        return s.presence
    }
}

/// Incremental SSE framing, bounded independently of HTTP chunk boundaries.
struct DetectionFramer {
    private var line = Data()
    private var event: String?
    private var id: String?
    private var payload: Data?
    mutating func append(_ chunk: Data, emit: (String, Data) throws -> Void) throws {
        for byte in chunk {
            if byte != 10 {
                guard line.count < 8192 else { throw MonitorError.responseTooLarge }
                line.append(byte); continue
            }
            if line.last == 13 { line.removeLast() }
            defer { line.removeAll(keepingCapacity: true) }
            if line.isEmpty {
                if let payload = payload {
                    guard event == "detection", let id = id, id.utf8.count <= 100 else { throw MonitorError.invalidContract }
                    try emit(id, payload)
                }
                event = nil; id = nil; payload = nil
            } else if line.first != 58 {
                guard let text = String(data: line, encoding: .utf8), let colon = text.firstIndex(of: ":") else { throw MonitorError.invalidContract }
                let name = text[..<colon]; var value = text[text.index(after: colon)...]
                if value.first == " " { value = value.dropFirst() }
                switch name {
                case "event": guard event == nil else { throw MonitorError.invalidContract }; event = String(value)
                case "id": guard id == nil else { throw MonitorError.invalidContract }; id = String(value)
                case "data": guard payload == nil else { throw MonitorError.invalidContract }; payload = Data(value.utf8)
                default: throw MonitorError.invalidContract
                }
            }
        }
    }
}
