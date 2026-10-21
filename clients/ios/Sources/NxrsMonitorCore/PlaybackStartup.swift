import Foundation

/// The existing native peer supplies this small boundary; tests can hold each SDK phase.
/// Async methods must cooperate with task cancellation. No pixels or VPN state cross it.
@MainActor public protocol PlaybackStartupPeer: AnyObject {
    func completeOffer() async throws -> String
    func apply(answer: String) async throws
    var hasDecodedVideo: Bool { get }
    var failed: Bool { get }
    func close()
}

public enum PlaybackStartupError: Error, Equatable, Sendable {
    case invalidTimeout, alreadyUsed, timedOut, peerFailed
}

/// Single-use startup owner. One monotonic budget covers offer/ICE, grant, WHEP,
/// remote answer and the first observed decoded frame. It is NOT a viewing lease.
/// Closing the local peer precedes remote cleanup; DELETE has its own bounded transport.
@MainActor public final class PlaybackStartup {
    public enum Stage: String, Sendable { case idle, offer, grant, whep, answer, firstFrame, ready }
    public private(set) var stage: Stage = .idle
    private enum State { case idle, starting, ready, closed }
    private var state: State = .idle
    private let peer: any PlaybackStartupPeer
    private let api: GatewayClient
    private let whep: WHEPClient
    private var remote: WHEPSession?
    private let onFailure: @MainActor (Error) -> Void

    public init(peer: any PlaybackStartupPeer, api: GatewayClient, whep: WHEPClient,
                onFailure: @escaping @MainActor (Error) -> Void = { _ in }) {
        self.peer = peer
        self.api = api
        self.whep = whep
        self.onFailure = onFailure
    }

    /// A timeout cancels pending SDK/HTTP work; no later phase may start afterward.
    /// Structured children are joined, not abandoned. This requires cancellation-aware
    /// dependencies (RTCOperation and BoundedHTTPTransport in the application).
    public func start(cameraID: ResourceID, credential: BearerCredential,
                      timeout: Duration = .seconds(15)) async throws {
        guard state == .idle else { throw PlaybackStartupError.alreadyUsed }
        state = .starting
        do {
            guard timeout > .zero, timeout <= .seconds(60) else {
                throw PlaybackStartupError.invalidTimeout
            }
            try Task.checkCancellation()
            let clock = ContinuousClock()
            let deadline = clock.now.advanced(by: timeout)
            try await withThrowingTaskGroup(of: Void.self) { group in
                group.addTask { @MainActor [self] in
                    try check(deadline, clock: clock)
                    stage = .offer
                    let offer = try await peer.completeOffer()
                    try check(deadline, clock: clock)
                    stage = .grant
                    let grant = try await api.playbackGrant(cameraID: cameraID, credential: credential)
                    try check(deadline, clock: clock)
                    stage = .whep
                    // Retain any returned resource before testing cancellation. If a timeout
                    // won concurrently, the outer cleanup must still delete this session.
                    remote = try await whep.open(grant: grant, offerSDP: offer)
                    try check(deadline, clock: clock)
                    guard let remote = remote else { throw WHEPError.invalidResponse }
                    stage = .answer
                    try await peer.apply(answer: remote.answerSDP)
                    try check(deadline, clock: clock)
                    stage = .firstFrame
                    while !peer.hasDecodedVideo {
                        try check(deadline, clock: clock)
                        guard !peer.failed else { throw PlaybackStartupError.peerFailed }
                        try await Task.sleep(for: .milliseconds(25))
                    }
                    try check(deadline, clock: clock)
                    guard !peer.failed else { throw PlaybackStartupError.peerFailed }
                }
                group.addTask {
                    try await clock.sleep(until: deadline, tolerance: .zero)
                    throw PlaybackStartupError.timedOut
                }
                do {
                    _ = try await group.next()
                    try check(deadline, clock: clock)
                    group.cancelAll() // Successful first frame retires the startup timer.
                } catch {
                    // Do this BEFORE joining a canceled child or attempting network DELETE.
                    abort(error)
                    group.cancelAll()
                    throw error
                }
            }
            try check(deadline, clock: clock)
            state = .ready
            stage = .ready
        } catch {
            abort(error)
            await close()
            throw error
        }
    }

    private func abort(_ error: Error) {
        guard state != .closed else { return }
        state = .closed
        peer.close()
        onFailure(error) // Notify UI before possibly slower, separately bounded DELETE.
    }

    private func check(_ deadline: ContinuousClock.Instant, clock: ContinuousClock) throws {
        try Task.checkCancellation()
        guard state == .starting else { throw CancellationError() }
        guard clock.now < deadline else { throw PlaybackStartupError.timedOut }
    }

    /// Idempotent resource retirement, also safe after a failed/canceled start.
    /// The caller cancels the task running start() when stopping during negotiation.
    @discardableResult public func close() async -> Bool {
        state = .closed
        peer.close()
        guard let remote = remote else { return true }
        self.remote = nil
        return await whep.close(remote)
    }
}
