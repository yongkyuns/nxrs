import Foundation
import SwiftUI
import NxrsMonitorCore
import NxrsMonitorNative

@MainActor final class MonitorModel: ObservableObject {
    @Published private(set) var enrolled = false
    @Published private(set) var cameras: [CameraSummary] = []
    @Published private(set) var selected: CameraSummary?
    @Published private(set) var peer: NativeVideoPeer?
    @Published private(set) var videoID = UUID()
    @Published private(set) var status = "Not connected"
    @Published private(set) var live = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var busy = false
    private let store = EnrollmentStore()
    private var enrollment: Enrollment?
    private var playback: Task<Void, Never>?
    private var catalog: Task<Void, Never>?
    private var generation = UUID()
    private var catalogGeneration = UUID()
    private var foreground = true

    func restore() {
        do {
            enrollment = try store.read()
            enrolled = enrollment != nil
            if enrolled { reload() }
        } catch { errorMessage = "Unlock the phone and retry. The saved enrollment could not be read." }
    }

    func enroll(gateway: String, media: String, token: String) {
        do {
            guard let base = URL(string: gateway.trimmingCharacters(in: .whitespacesAndNewlines)) else {
                throw EnrollmentFailure.invalid
            }
            let extra = media.trimmingCharacters(in: .whitespacesAndNewlines)
            let origin = extra.isEmpty ? nil : URL(string: extra)
            guard extra.isEmpty || origin != nil else { throw EnrollmentFailure.invalid }
            let record = Enrollment(gateway: base, mediaOrigin: origin,
                token: token.trimmingCharacters(in: .whitespacesAndNewlines))
            _ = try record.policy()
            try store.save(record)
            stop()
            enrollment = record
            enrolled = true
            cameras = []
            selected = nil
            errorMessage = nil
            reload()
        } catch { errorMessage = "Use a trusted HTTPS gateway and valid viewer credential. Check Keychain access." }
    }

    func forget() {
        stop()
        catalog?.cancel()
        catalogGeneration = UUID()
        busy = false
        do {
            try store.remove()
            enrollment = nil
            enrolled = false
            cameras = []
            selected = nil
            errorMessage = nil
        } catch { errorMessage = "Could not remove the saved credential. Unlock the phone and retry." }
    }

    func reload() {
        guard let enrollment = enrollment else { return }
        catalog?.cancel()
        let ticket = UUID()
        catalogGeneration = ticket
        busy = true
        catalog = Task { [weak self] in
            do {
                let client = try GatewayClient(policy: enrollment.policy())
                let cameras = try await client.cameras(credential: BearerCredential(enrollment.token))
                try Task.checkCancellation()
                guard let self = self, self.catalogGeneration == ticket else { return }
                self.cameras = cameras
                self.busy = false
                self.errorMessage = nil
                if let selected = self.selected, !cameras.contains(where: { $0.cameraID == selected.cameraID }) {
                    self.stop()
                    self.selected = nil
                }
            } catch {
                guard let self = self, !Task.isCancelled, self.catalogGeneration == ticket else { return }
                self.busy = false
                self.errorMessage = self.message(for: error)
            }
        }
    }

    func select(_ camera: CameraSummary) {
        stop()
        selected = camera
        guard foreground, camera.liveVideo, let enrollment = enrollment else { return }
        let ticket = generation
        status = "Connecting"
        errorMessage = nil
        playback = Task { [weak self] in
            guard let self = self else { return }
            await self.receive(camera, enrollment: enrollment, ticket: ticket)
        }
    }

    func setForeground(_ active: Bool) {
        foreground = active
        if !active {
            stop()
            catalog?.cancel()
            catalogGeneration = UUID()
            busy = false
        } else {
            reload()
            if let selected = selected { select(selected) }
        }
    }

    func stop() {
        generation = UUID()
        playback?.cancel()
        playback = nil
        peer?.close() // Stop decoding immediately, not after a network DELETE completes.
        peer = nil
        live = false
        status = "Stopped"
    }

    private func receive(_ camera: CameraSummary, enrollment: Enrollment, ticket: UUID) async {
        guard let policy = try? enrollment.policy(), let credential = try? BearerCredential(enrollment.token) else { return }
        let api = GatewayClient(policy: policy)
        let whep = WHEPClient(policy: policy)
        // Bounded automatic retries. A fresh peer, grant and startup budget are used every time.
        for attempt in 0..<4 {
            guard !Task.isCancelled, generation == ticket, foreground else { return }
            if attempt > 0 {
                status = "Reconnecting (\(attempt)/3)"
                do { try await Task.sleep(nanoseconds: UInt64(1 << attempt) * 1_000_000_000) }
                catch { return }
                guard generation == ticket, foreground else { return }
            }
            let currentPeer = NativeVideoPeer()
            peer = currentPeer
            videoID = UUID()
            live = false
            let startup = PlaybackStartup(peer: currentPeer, api: api, whep: whep) { [weak self] error in
                guard let self = self, self.generation == ticket, !Task.isCancelled, self.foreground else { return }
                self.peer = nil
                self.live = false
                self.status = (error as? PlaybackStartupError) == .timedOut
                    ? "Connection timed out" : "Connection failed"
            }
            var failure: Error?
            do {
                // One deadline covers ICE, grant, POST, answer and first decoded frame.
                // Startup closes locally and updates status before awaiting remote cleanup.
                try await startup.start(cameraID: camera.cameraID, credential: credential)
                try Task.checkCancellation()
                guard generation == ticket else { throw CancellationError() }
                var lifecycle = try PlaybackLifecycle()
                let sessionID = try lifecycle.begin(now: ProcessInfo.processInfo.systemUptime)
                var count: UInt64 = 0
                var stalledAt: TimeInterval?
                while true {
                    try Task.checkCancellation()
                    guard generation == ticket else { throw CancellationError() }
                    let now = ProcessInfo.processInfo.systemUptime
                    let frames = currentPeer.frames
                    if frames.count != count {
                        count = frames.count
                        lifecycle.decodedFrame(session: sessionID, now: now)
                        stalledAt = nil
                    }
                    lifecycle.tick(now: now)
                    switch lifecycle.state {
                    case .live: status = "Live"; live = true
                    case .stalled:
                        status = "Video stalled — image is not current"
                        live = false
                        if stalledAt == nil { stalledAt = now }
                    case .offline: throw NativePeerError.timedOut
                    default: status = "Connecting"; live = false
                    }
                    if currentPeer.failed || stalledAt.map({ now - $0 >= 5 }) == true {
                        throw NativePeerError.unavailable
                    }
                    try await Task.sleep(nanoseconds: 250_000_000)
                }
            } catch { failure = error }
            currentPeer.close()
            if generation == ticket { peer = nil; live = false }
            await startup.close()
            guard generation == ticket, !Task.isCancelled, foreground else { return }
            if let failure = failure {
                let terminal = isAuthorizationFailure(failure)
                if terminal || attempt == 3 {
                    status = terminal ? "Access denied" : "Offline"
                    errorMessage = message(for: failure)
                    return
                }
            }
        }
    }

    private func isAuthorizationFailure(_ error: Error) -> Bool {
        guard let error = error as? MonitorError else { return false }
        return error == .unauthenticated || error == .forbidden || error == .untrustedOrigin
            || error == .invalidEndpoint || error == .invalidContract || error == .notFound
            || error == .rateLimited
    }

    private func message(for error: Error) -> String {
        if (error as? PlaybackStartupError) == .timedOut {
            return "No video arrived within the 15-second connection limit. Check gateway and WireGuard connectivity, then reconnect."
        }
        switch error as? MonitorError {
        case .unauthenticated, .forbidden:
            return "Access was denied. Check or replace the saved viewer credential."
        case .untrustedOrigin, .invalidEndpoint:
            return "The video endpoint is not trusted. Review gateway enrollment; no credential was sent to it."
        case .rateLimited:
            return "The gateway admission limit was reached. Wait before reconnecting."
        case .notFound: return "This camera is no longer available to your account."
        default:
            return "Connection failed. Check the gateway certificate, network access and WireGuard tunnel, then reconnect."
        }
    }
}
