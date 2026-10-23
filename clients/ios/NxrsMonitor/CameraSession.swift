import Foundation
import SwiftUI
import NxrsMonitorCore
import NxrsMonitorNative

/// One immutable home/enrollment binding and one camera's resources. Local
/// teardown is immediate; admission retains the slot until remote cleanup joins.
@MainActor final class CameraSession: ObservableObject {
    private(set) var camera: CameraSummary
    @Published private(set) var peer: NativeVideoPeer?
    @Published private(set) var videoID = UUID()
    @Published private(set) var status = "Paused"
    @Published private(set) var live = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var detectionStatus = "Detection unavailable"
    @Published private(set) var personDetected = false
    @Published private(set) var detectionHistoryIncomplete = false
    @Published private(set) var snapshotBusy = false
    @Published private(set) var recordingSince: Date?
    @Published private(set) var finalizingClip = false
    @Published private(set) var resolution = ""
    @Published private(set) var framesPerSecond = 0
    @Published private(set) var active = false
    private(set) var retiring = false
    private(set) var blocked = false
    var occupiesSlot: Bool { active || retiring }
    private let detectionSession = DetectionSession()
    private let enrollment: Enrollment?
    private let settings: HomeSettings
    private let library: HomeLibrary
    private let notify: (String) -> Void
    private let released: () -> Void
    private let changed: () -> Void
    private var playback: Task<Void, Never>?
    private var generation = UUID()
    private var runID: UUID?
    private var snapshotTask: Task<Void, Never>?
    private var recordingPreparation: Task<Void, Never>?
    private var finalization: Task<Void, Never>?
    private var recordingStarting = false
    private var recording: Recording?
    private struct Recording {
        let recorder: NativeClipRecorder
        let url: URL
        let cameraID: String
        let cameraName: String
        let started: TimeInterval
    }
    init(camera: CameraSummary, enrollment: Enrollment?, settings: HomeSettings,
         library: HomeLibrary, notify: @escaping (String) -> Void, released: @escaping () -> Void,
         changed: @escaping () -> Void) {
        self.camera = camera; self.enrollment = enrollment; self.settings = settings
        self.library = library; self.notify = notify; self.released = released; self.changed = changed
    }
    var canRecord: Bool { live && !snapshotBusy && !recordingStarting && !finalizingClip }
    var isRecording: Bool { recordingSince != nil }
    func update(_ camera: CameraSummary) { precondition(camera.cameraID == self.camera.cameraID); self.camera = camera }
    func allowRetry() { blocked = false }
    func start() {
        guard !occupiesSlot, !blocked, let enrollment else { return }
        let ticket = UUID(); generation = ticket; runID = ticket
        active = true; errorMessage = nil; setStatus("Connecting", live: false)
        playback = Task { [weak self] in
            guard let self else { return }
            await self.receive(self.camera, enrollment: enrollment, ticket: ticket)
            self.retiring = true; self.active = false
            await self.snapshotTask?.value
            await self.recordingPreparation?.value
            await self.finalization?.value
            self.finish(ticket)
        }
        if let policy = try? enrollment.policy(), let credential = try? BearerCredential(enrollment.token) {
            detectionSession.start(client: DetectionClient(policy: policy), cameraID: camera.cameraID, credential: credential) { [weak self] in
                guard let self, self.generation == ticket else { return }
                self.blocked = true; self.stop(); self.setStatus("Access denied", live: false)
            }
        }
    }
    func stop() {
        active = false; generation = UUID()
        endRecording(); detectionSession.stop()
        peer?.close(); peer = nil
        retiring = playback != nil
        playback?.cancel()
        setStatus("Paused", live: false)
        resolution = ""; framesPerSecond = 0
    }
    private func finish(_ ticket: UUID) {
        guard runID == ticket else { return }
        playback = nil; runID = nil; retiring = false; active = false
        released()
    }
    private func showToast(_ text: String) { notify(text) }
    func takeSnapshot() {
        guard live, !snapshotBusy, let peer else { return }
        snapshotBusy = true
        let name = settings.preferences.displayName(for: camera)
        snapshotTask = Task { [weak self] in
            guard let self else { return }
            defer { self.snapshotBusy = false; self.snapshotTask = nil }
            do {
                let data = try await peer.snapshotJPEG()
                _ = try await self.library.repository.savePhoto(data, cameraID: camera.cameraID.value, cameraName: name)
                await self.library.reload()
                self.showToast("Snapshot saved to Recordings")
                UINotificationFeedbackGenerator().notificationOccurred(.success)
            } catch { self.showToast(self.captureMessage(error)) }
        }
    }

    func toggleRecording() {
        if isRecording { endRecording(); return }
        guard canRecord, let peer else { return }
        recordingStarting = true
        let ticket = generation
        let name = settings.preferences.displayName(for: camera)
        recordingPreparation = Task { [weak self] in
            guard let self else { return }
            defer { self.recordingStarting = false; self.recordingPreparation = nil }
            var staging: URL?
            do {
                let url = try await self.library.repository.reserveClip()
                staging = url
                guard self.generation == ticket, self.live else {
                    try await self.library.repository.discardClip(at: url); return
                }
                let recorder = try peer.startRecording(to: url)
                self.recording = Recording(recorder: recorder, url: url,
                    cameraID: camera.cameraID.value, cameraName: name,
                    started: ProcessInfo.processInfo.systemUptime)
                self.recordingSince = Date()
                UIImpactFeedbackGenerator(style: .light).impactOccurred()
            } catch {
                if let staging { try? await self.library.repository.discardClip(at: staging) }
                self.showToast(self.captureMessage(error))
            }
        }
    }

    private func endRecording() {
        guard let current = recording else { return }
        recording = nil; recordingSince = nil; finalizingClip = true
        let background = UIApplication.shared.beginBackgroundTask(withName: "Save camera clip", expirationHandler: nil)
        finalization = Task { [weak self] in
            guard let self else { return }
            defer {
                self.finalizingClip = false; self.finalization = nil
                if background != .invalid { UIApplication.shared.endBackgroundTask(background) }
            }
            do {
                let result = try await current.recorder.finish()
                _ = try await self.library.repository.finishClip(at: current.url, duration: result.duration,
                    cameraID: current.cameraID, cameraName: current.cameraName)
                await self.library.reload()
                self.showToast("Video clip saved to Recordings")
            } catch {
                try? await self.library.repository.discardClip(at: current.url)
                self.showToast(self.captureMessage(error))
            }
        }
    }

    private func setStatus(_ text: String, live: Bool) {
        if status != text { status = text }
        if self.live != live { self.live = live; changed() }
        refreshDetections()
    }

    private func receive(_ camera: CameraSummary, enrollment: Enrollment, ticket: UUID) async {
        guard let policy = try? enrollment.policy(), let credential = try? BearerCredential(enrollment.token) else {
            blocked = true; setStatus("Access denied", live: false); return
        }
        let api = GatewayClient(policy: policy)
        let whep = WHEPClient(policy: policy)
        for attempt in 0..<4 {
            guard !Task.isCancelled, generation == ticket, active else { return }
            if attempt > 0 {
                setStatus("Reconnecting · \(attempt) of 3", live: false)
                do { try await Task.sleep(nanoseconds: UInt64(1 << attempt) * 1_000_000_000) }
                catch { return }
                guard generation == ticket, active else { return }
            }
            let currentPeer = NativeVideoPeer()
            peer = currentPeer; videoID = UUID(); setStatus("Connecting", live: false)
            let startup = PlaybackStartup(peer: currentPeer, api: api, whep: whep) { [weak self] error in
                guard let self, self.generation == ticket, !Task.isCancelled, self.active else { return }
                self.peer = nil
                self.setStatus((error as? PlaybackStartupError) == .timedOut ? "Connection timed out" : "Connection failed", live: false)
            }
            var failure: Error?
            do {
                try await startup.start(cameraID: camera.cameraID, credential: credential)
                try Task.checkCancellation()
                guard generation == ticket else { throw CancellationError() }
                var lifecycle = try PlaybackLifecycle()
                let sessionID = try lifecycle.begin(now: ProcessInfo.processInfo.systemUptime)
                var count: UInt64 = 0
                var stalledAt: TimeInterval?
                var sampleTime = ProcessInfo.processInfo.systemUptime
                var sampleCount: UInt64 = 0
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
                    case .live: setStatus("Live", live: true)
                    case .stalled:
                        setStatus("Video interrupted", live: false)
                        if stalledAt == nil { stalledAt = now; endRecording() }
                    case .offline: throw NativePeerError.timedOut
                    default: setStatus("Connecting", live: false)
                    }
                    if now - sampleTime >= 1 {
                        resolution = "\(frames.width) × \(frames.height)"
                        framesPerSecond = Int(Double(frames.count - sampleCount) / (now - sampleTime))
                        sampleTime = now; sampleCount = frames.count
                    }
                    if let recording, now - recording.started >= 60 || recording.recorder.failed { endRecording() }
                    if currentPeer.failed || stalledAt.map({ now - $0 >= 5 }) == true { throw NativePeerError.unavailable }
                    refreshDetections()
                    try await Task.sleep(nanoseconds: 250_000_000)
                }
            } catch { failure = error }
            if generation == ticket { endRecording() }
            currentPeer.close()
            if generation == ticket { peer = nil; setStatus("Reconnecting", live: false) }
            await startup.close()
            guard generation == ticket, !Task.isCancelled, active else { return }
            if let failure, isAuthorizationFailure(failure) || attempt == 3 {
                setStatus(isAuthorizationFailure(failure) ? "Access denied" : "Offline", live: false)
                errorMessage = Self.message(for: failure)
                blocked = true; active = false
                return
            }
        }
    }

    private func refreshDetections() {
        detectionHistoryIncomplete = detectionSession.eventGapObserved
        let now = ProcessInfo.processInfo.systemUptime
        let presence: DetectionPresence
        if live, let frame = peer?.frames, let arrived = frame.lastArrival,
           now >= arrived, now - arrived < 1.5, let metadata = frame.videoMetadata {
            presence = metadata.presence(additionalAge: now - arrived)
        } else {
            presence = detectionSession.presentation.presence(now: now, videoLive: live)
        }
        personDetected = presence == .person
        switch presence {
        case .person: detectionStatus = "Person detected"
        case .clear: detectionStatus = "No person detected"
        case .unavailable: detectionStatus = "Detection unavailable"
        }
    }

    private func isAuthorizationFailure(_ error: Error) -> Bool {
        guard let error = error as? MonitorError else { return false }
        return [.unauthenticated, .forbidden, .untrustedOrigin, .invalidEndpoint,
                .invalidContract, .notFound, .rateLimited].contains(error)
    }

    static func message(for error: Error) -> String {
        if (error as? WHEPError) == .unavailable {
            return "Your camera's video stream is unavailable. Check that the camera is powered on and streaming to your home gateway, then reconnect."
        }
        if (error as? PlaybackStartupError) == .timedOut {
            return "Your camera did not send video in time. Check that your home and private-network connection are available, then reconnect."
        }
        if let network = error as? URLError {
            switch network.code {
            case .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot,
                 .serverCertificateNotYetValid, .secureConnectionFailed:
                return "The home certificate could not be verified. Check its trust and expiry before trying again."
            case .notConnectedToInternet, .networkConnectionLost, .cannotConnectToHost, .cannotFindHost:
                return "Your home could not be reached. Check Wi-Fi or your private-network app, then try again."
            default: break
            }
        }
        switch error as? MonitorError {
        case .unauthenticated, .forbidden: return "This viewer key cannot access your home. Ask the home owner for a new invitation."
        case .untrustedOrigin, .invalidEndpoint: return "The video address is not trusted. Check your home invitation before reconnecting."
        case .rateLimited: return "Your home is busy with other connections. Wait a moment, then reconnect."
        case .notFound: return "This camera is no longer shared with your viewer key."
        default: return "Your home could not be reached. Check its certificate, network access and private-network connection, then retry."
        }
    }

    private func captureMessage(_ error: Error) -> String {
        switch error as? CaptureLibraryError {
        case .full: return "Recording storage is full. Delete saved footage and try again."
        default:
            if (error as? NativeCaptureError) == .noCurrentFrame { return "Wait for live video before saving footage." }
            if (error as? NativeCaptureError) == .noRecordedFrames { return "The clip was too short to save. Record a little longer." }
            return "This recording could not be saved. Check your iPhone storage and try again."
        }
    }

}
