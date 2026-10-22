import Foundation

/// One foreground camera subscription. The app and native qualification probe
/// use the same retry, cancellation and generation barriers.
@MainActor public final class DetectionSession {
    public private(set) var presentation = DetectionState()
    public private(set) var messageCount = 0
    public private(set) var reconnects = 0
    public private(set) var eventGapObserved = false
    public private(set) var lastError: MonitorError?
    private var task: Task<Void, Never>?
    private var generation = UUID()
    public init() {}
    deinit { task?.cancel() }
    public func stop() {
        generation = UUID(); task?.cancel(); task = nil
        presentation.reset(); messageCount = 0; reconnects = 0; eventGapObserved = false; lastError = nil
    }
    public func start(client: any DetectionFeedClient, cameraID: ResourceID, credential: BearerCredential,
                      onAuthorizationFailure: @escaping @MainActor @Sendable () -> Void = {}) {
        stop(); let ticket = generation
        task = Task { [weak self] in
            var failures = 0; var cursor: String?; var attempt = 0
            while !Task.isCancelled, self?.generation == ticket {
                let started = ProcessInfo.processInfo.systemUptime
                if attempt > 0 { self?.reconnects += 1 }
                attempt += 1
                do {
                    for try await message in client.subscribe(cameraID: cameraID, credential: credential, cursor: cursor) {
                        try Task.checkCancellation()
                        guard let owner = self, owner.generation == ticket else { return }
                        try owner.presentation.receive(message, cameraID: cameraID)
                        owner.messageCount += 1
                        owner.eventGapObserved = owner.eventGapObserved || owner.presentation.eventGap
                        owner.lastError = nil; cursor = message.cursor
                    }
                } catch {
                    guard !Task.isCancelled, self?.generation == ticket else { return }
                    if let error = error as? MonitorError {
                        self?.lastError = error
                        if [.unauthenticated, .forbidden, .notFound, .invalidContract, .invalidEndpoint,
                            .untrustedOrigin, .redirectRejected, .rateLimited, .responseTooLarge].contains(error) {
                            self?.presentation.reset()
                            if error == .unauthenticated || error == .forbidden { onAuthorizationFailure() }
                            return
                        }
                    }
                }
                guard !Task.isCancelled, self?.generation == ticket else { return }
                self?.presentation.reset()
                failures = ProcessInfo.processInfo.systemUptime - started >= 3 ? 0 : failures + 1
                if failures >= 4 { return }
                do { try await Task.sleep(nanoseconds: UInt64(1 << min(failures + 1, 3)) * 1_000_000_000) }
                catch { return }
            }
        }
    }
}
