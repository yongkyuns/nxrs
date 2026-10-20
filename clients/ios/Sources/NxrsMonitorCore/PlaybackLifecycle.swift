import Foundation

/// Pure state reducer; the eventual player owner performs networking/cleanup and serializes calls.
/// `now` is monotonic seconds (for example systemUptime), never wall-clock Date or camera time.
public struct PlaybackLifecycle: Sendable {
    public enum State: String, Sendable { case idle, connecting, live, stalled, reconnecting, offline, accessDenied }
    public private(set) var state: State = .idle
    public private(set) var session: UUID?
    private var startedAt: TimeInterval = 0
    private var lastFrameAt: TimeInterval?
    private var lastEventAt: TimeInterval = 0
    private let stallAfter: TimeInterval
    private let startupTimeout: TimeInterval

    public init(stallAfter: TimeInterval = 2, startupTimeout: TimeInterval = 15) throws {
        guard stallAfter.isFinite, startupTimeout.isFinite, stallAfter > 0, startupTimeout > 0 else {
            throw MonitorError.invalidTime
        }
        self.stallAfter = stallAfter
        self.startupTimeout = startupTimeout
    }

    @discardableResult public mutating func begin(now: TimeInterval) throws -> UUID {
        guard now.isFinite, now >= 0, now >= lastEventAt else { throw MonitorError.invalidTime }
        let id = UUID()
        session = id
        startedAt = now
        lastEventAt = now
        lastFrameAt = nil
        state = .connecting
        return id
    }

    /// A signaling/VPN handshake must never call this. Only the decoder's frame callback may.
    @discardableResult public mutating func decodedFrame(session id: UUID, now: TimeInterval) -> Bool {
        guard session == id, now.isFinite, now >= lastEventAt else { return false }
        lastEventAt = now
        lastFrameAt = now
        state = .live
        return true
    }

    public mutating func tick(now: TimeInterval) {
        guard session != nil, now.isFinite, now >= lastEventAt else { return }
        lastEventAt = now
        if let last = lastFrameAt {
            if now - last >= stallAfter { state = .stalled }
        } else if now - startedAt >= startupTimeout {
            retire(as: .offline)
        }
    }

    public mutating func disconnected(session id: UUID) {
        guard session == id else { return }
        retire(as: .reconnecting)
    }
    public mutating func denied(session id: UUID) {
        guard session == id else { return }
        retire(as: .accessDenied)
    }
    /// Used on camera deselection, cancellation, and backgrounding.
    public mutating func stop() { retire(as: .idle) }
    private mutating func retire(as state: State) {
        session = nil
        lastFrameAt = nil
        self.state = state
    }
}
