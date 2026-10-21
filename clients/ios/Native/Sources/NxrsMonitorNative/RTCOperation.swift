import Foundation

public enum NativePeerError: Error, Sendable { case unavailable, negotiationFailed, timedOut }

/// One-shot bridge for SDK callbacks. Cancellation, timeout and completion can race.
/// Resume exactly once outside the lock; never echo SDK errors or SDP.
final class RTCOperation<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Value, Error>?
    private var result: Result<Value, Error>?
    private var timer: DispatchWorkItem?

    @MainActor func run(timeout: TimeInterval = 8,
                       start: (@escaping (Result<Value, Error>) -> Void) -> Void) async throws -> Value {
        try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { continuation in
                lock.lock()
                if let result = result {
                    lock.unlock()
                    continuation.resume(with: result)
                    return
                }
                self.continuation = continuation
                let timer = DispatchWorkItem { [weak self] in self?.finish(.failure(NativePeerError.timedOut)) }
                self.timer = timer
                lock.unlock()
                DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: timer)
                start { self.finish($0) }
            }
        }, onCancel: { self.finish(.failure(CancellationError())) })
    }

    func finish(_ result: Result<Value, Error>) {
        lock.lock()
        guard self.result == nil else { lock.unlock(); return }
        self.result = result
        let continuation = self.continuation
        let timer = self.timer
        self.continuation = nil
        self.timer = nil
        lock.unlock()
        timer?.cancel()
        continuation?.resume(with: result)
    }
}
