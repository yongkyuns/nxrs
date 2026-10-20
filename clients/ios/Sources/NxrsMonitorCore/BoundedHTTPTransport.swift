import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// Foreground, bounded, cancellable HTTPS requests. No cookies, credential cache, or redirects.
public struct BoundedHTTPTransport: HTTPTransport {
    public static let maximumResponseBytes = 1_048_576
    private let protocols: [AnyClass]?
    public init() { protocols = nil }
    init(protocols: [AnyClass]) { self.protocols = protocols }
    public func send(_ request: URLRequest) async throws -> HTTPResult {
        guard request.url?.scheme?.lowercased() == "https" else { throw MonitorError.invalidEndpoint }
        return try await Transfer(request: request, limit: Self.maximumResponseBytes, protocols: protocols).run()
    }
}

// URLSession delegates and task cancellation run on different queues. All mutable state
// is protected by lock; continuations are resumed exactly once, outside the lock.
private final class Transfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private let request: URLRequest
    private let limit: Int
    private let protocols: [AnyClass]?
    private var continuation: CheckedContinuation<HTTPResult, Error>?
    private var task: URLSessionDataTask?
    private var session: URLSession?
    private var response: HTTPURLResponse?
    private var data = Data()
    private var finished = false

    init(request: URLRequest, limit: Int, protocols: [AnyClass]?) {
        self.request = request; self.limit = limit; self.protocols = protocols
    }

    func run() async throws -> HTTPResult {
        try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { start($0) }
        }, onCancel: { self.finish(.failure(CancellationError())) })
    }

    private func start(_ continuation: CheckedContinuation<HTTPResult, Error>) {
        lock.lock()
        guard !finished else { lock.unlock(); continuation.resume(throwing: CancellationError()); return }
        self.continuation = continuation
        let config = URLSessionConfiguration.ephemeral
        if let protocols = protocols { config.protocolClasses = protocols }
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.urlCredentialStorage = nil
        config.httpShouldSetCookies = false
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 20
        let session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
        let task = session.dataTask(with: request)
        self.session = session
        self.task = task
        lock.unlock()
        task.resume()
    }

    private func finish(_ result: Result<HTTPResult, Error>) {
        lock.lock()
        guard !finished else { lock.unlock(); return }
        finished = true
        let continuation = self.continuation
        let session = self.session
        self.continuation = nil
        self.session = nil
        self.task = nil
        data.removeAll(keepingCapacity: false)
        lock.unlock()
        session?.invalidateAndCancel()
        continuation?.resume(with: result)
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
                    completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
        finish(.failure(MonitorError.redirectRejected))
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse else {
            completionHandler(.cancel); finish(.failure(MonitorError.invalidContract)); return
        }
        guard response.expectedContentLength <= Int64(limit) else {
            completionHandler(.cancel); finish(.failure(MonitorError.responseTooLarge)); return
        }
        lock.lock()
        let active = !finished
        if active { self.response = http }
        lock.unlock()
        completionHandler(active ? .allow : .cancel)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive chunk: Data) {
        lock.lock()
        guard !finished else { lock.unlock(); return }
        let tooLarge = chunk.count > limit - data.count
        if !tooLarge { data.append(chunk) }
        lock.unlock()
        if tooLarge { finish(.failure(MonitorError.responseTooLarge)) }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let result: Result<HTTPResult, Error>
        if let error = error { result = .failure(error) }
        else if let response = response { result = .success(HTTPResult(data: data, response: response)) }
        else { result = .failure(MonitorError.invalidContract) }
        lock.unlock()
        finish(result)
    }
}
