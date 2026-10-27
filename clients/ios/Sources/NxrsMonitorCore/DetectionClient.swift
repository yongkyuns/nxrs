import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// The sequence/iterator owns cancellation, including early loop exit. The
/// URLSession delegate does not retain this lease, so there is no owner cycle.
public struct DetectionSubscription: AsyncSequence, Sendable {
    public typealias Element = DetectionMessage
    fileprivate final class Lease: @unchecked Sendable {
        let cancel: @Sendable () -> Void
        init(_ cancel: @escaping @Sendable () -> Void) { self.cancel = cancel }
        deinit { cancel() }
    }
    fileprivate let events: AsyncThrowingStream<DetectionMessage, Error>
    fileprivate let lease: Lease
    init(_ events: AsyncThrowingStream<DetectionMessage, Error>, cancel: @escaping @Sendable () -> Void = {}) {
        self.events = events; lease = Lease(cancel)
    }
    public struct AsyncIterator: AsyncIteratorProtocol {
        fileprivate var base: AsyncThrowingStream<DetectionMessage, Error>.Iterator
        fileprivate let lease: Lease
        public mutating func next() async throws -> DetectionMessage? { try await base.next() }
    }
    public func makeAsyncIterator() -> AsyncIterator { AsyncIterator(base: events.makeAsyncIterator(), lease: lease) }
    public func cancel() { lease.cancel() }
    fileprivate static func failed(_ error: Error) -> Self { Self(AsyncThrowingStream { $0.finish(throwing: error) }) }
}

public protocol DetectionFeedClient: Sendable {
    func subscribe(cameraID: ResourceID, credential: BearerCredential, cursor: String?) -> DetectionSubscription
}
public struct DetectionClient: DetectionFeedClient {
    public let policy: EndpointPolicy
    public init(policy: EndpointPolicy) { self.policy = policy }
    public func subscribe(cameraID: ResourceID, credential: BearerCredential, cursor: String? = nil)
        -> DetectionSubscription {
        if let cursor = cursor, (cursor.utf8.count > 100 || !cursor.utf8.allSatisfy({
            (48...57).contains($0) || (97...122).contains($0) || $0 == 58
        })) { return .failed(MonitorError.invalidContract) }
        var request = URLRequest(url: policy.apiURL(["v1", "cameras", cameraID.value, "detections"]))
        request.httpMethod = "GET"; request.timeoutInterval = 15
        request.setValue("Bearer \(credential.value)", forHTTPHeaderField: "Authorization")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.setValue("no-store", forHTTPHeaderField: "Cache-Control")
        if let cursor = cursor { request.setValue(cursor, forHTTPHeaderField: "Last-Event-ID") }
        return DetectionTransfer.open(request, cameraID: cameraID)
    }
}

/// The serial delegate parses bounded events. Cancellation can run on another
/// queue; its lock owns terminal state and the single latest-value continuation.
final class DetectionTransfer: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let lock = NSLock()
    private let cameraID: ResourceID
    private let started = ProcessInfo.processInfo.systemUptime
    private var continuation: AsyncThrowingStream<DetectionMessage, Error>.Continuation?
    private var session: URLSession?
    private var finished = false
    private var framer = DetectionFramer()
    private var lastElapsed: UInt64 = 0
    private init(_ cameraID: ResourceID) { self.cameraID = cameraID }
    static func open(_ request: URLRequest, cameraID: ResourceID, protocols: [AnyClass]? = nil)
        -> DetectionSubscription {
        guard request.url?.scheme?.lowercased() == "https" else {
            return .failed(MonitorError.invalidEndpoint)
        }
        if Task.isCancelled { return .failed(CancellationError()) }
        let owner = DetectionTransfer(cameraID)
        let events = AsyncThrowingStream<DetectionMessage, Error>(bufferingPolicy: .bufferingNewest(1)) { continuation in
            continuation.onTermination = { @Sendable [weak owner] _ in owner?.finish(nil) }
            owner.continuation = continuation
            let config = URLSessionConfiguration.ephemeral
            config.protocolClasses = protocols
            config.urlCache = nil; config.httpCookieStorage = nil; config.urlCredentialStorage = nil
            config.httpShouldSetCookies = false
            config.timeoutIntervalForRequest = 15; config.timeoutIntervalForResource = 300
            let session = URLSession(configuration: config, delegate: owner, delegateQueue: nil)
            owner.session = session
            session.dataTask(with: request).resume()
        }
        return DetectionSubscription(events, cancel: { @Sendable [weak owner] in owner?.finish(nil) })
    }
    private func finish(_ error: Error?) {
        lock.lock()
        guard !finished else { lock.unlock(); return }
        finished = true
        let continuation = self.continuation; let session = self.session
        self.continuation = nil; self.session = nil
        lock.unlock()
        session?.invalidateAndCancel()
        if let error = error { continuation?.finish(throwing: error) } else { continuation?.finish() }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil); finish(MonitorError.redirectRejected)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping @Sendable (URLSession.ResponseDisposition) -> Void) {
        guard let http = response as? HTTPURLResponse else { completionHandler(.cancel); finish(MonitorError.invalidContract); return }
        let status = http.statusCode
        if status != 200 {
            let error: MonitorError
            switch status {
            case 401: error = .unauthenticated
            case 403: error = .forbidden
            case 404: error = .notFound
            case 429: error = .rateLimited
            case 300...399: error = .redirectRejected
            default: error = .serviceUnavailable
            }
            completionHandler(.cancel); finish(error); return
        }
        guard http.value(forHTTPHeaderField: "Content-Type")?.split(separator: ";").first?
            .trimmingCharacters(in: .whitespaces).lowercased() == "text/event-stream" else {
            completionHandler(.cancel); finish(MonitorError.invalidContract); return
        }
        lock.lock(); let active = !finished; lock.unlock()
        completionHandler(active ? .allow : .cancel)
    }
    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock(); let active = !finished; lock.unlock()
        guard active else { return }
        guard data.count <= 65536 else { finish(MonitorError.responseTooLarge); return }
        do {
            try framer.append(data) { cursor, bytes in
                let snapshot = try JSONDecoder().decode(DetectionSnapshot.self, from: bytes)
                try snapshot.validate(cameraID: cameraID)
                guard snapshot.stream_elapsed_ms >= lastElapsed,
                      cursor == "\(snapshot.source_session_id ?? "none"):\(snapshot.source?.event_count ?? 0)" else {
                    throw MonitorError.invalidContract
                }
                lastElapsed = snapshot.stream_elapsed_ms
                let message = DetectionMessage(snapshot: snapshot, cursor: cursor,
                    receivedAt: ProcessInfo.processInfo.systemUptime, requestStartedAt: started)
                lock.lock(); let continuation = self.continuation; lock.unlock()
                continuation?.yield(message)
            }
        } catch { finish((error as? MonitorError) ?? MonitorError.invalidContract) }
    }
    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) { finish(error) }
}
