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
    @Published private(set) var status = "Choose a camera"
    @Published private(set) var live = false
    @Published private(set) var errorMessage: String?
    @Published private(set) var busy = false
    @Published private(set) var pairing = false
    @Published private(set) var pairingError: String?
    @Published private(set) var enrollmentID = UUID()
    @Published private(set) var catalogCheckedAt: Date?
    @Published private(set) var snapshotBusy = false
    @Published private(set) var recordingSince: Date?
    @Published private(set) var finalizingClip = false
    @Published private(set) var resolution = ""
    @Published private(set) var framesPerSecond = 0
    @Published private(set) var toast: String?
    private(set) var isPreview = false
    let settings: HomeSettings
    let library: HomeLibrary
    private let store = EnrollmentStore()
    private var enrollment: Enrollment?
    private var playback: Task<Void, Never>?
    private var catalog: Task<Void, Never>?
    private var pairingTask: Task<Void, Never>?
    private var toastTask: Task<Void, Never>?
    private var generation = UUID()
    private var catalogGeneration = UUID()
    private var foreground = true
    private var resumeWhenActive = false
    private var restored = false
    private var autoSelectionPending = false
    private var recordingStarting = false
    private var recording: Recording?
    private struct Recording {
        let recorder: NativeClipRecorder
        let url: URL
        let cameraID: String
        let cameraName: String
        let started: TimeInterval
    }

    init(settings: HomeSettings? = nil, library: HomeLibrary? = nil) {
        self.settings = settings ?? HomeSettings()
        self.library = library ?? HomeLibrary()
    }

    var gatewayHost: String { enrollment?.gateway.host ?? "No home connected" }
    var gatewayAddress: String { enrollment?.gateway.absoluteString ?? "" }
    var canRecord: Bool { live && !snapshotBusy && !recordingStarting && !finalizingClip }
    var isRecording: Bool { recordingSince != nil }
    var connectionSummary: String {
        if live { return "Receiving video" }
        if busy { return "Checking connection" }
        if errorMessage != nil { return "Connection needs attention" }
        if catalogCheckedAt != nil { return "Home connected" }
        return enrolled ? "Connection needs attention" : "Connect your home"
    }

    func restore() {
        guard !restored else { return }
        restored = true
        Task { await library.reload() }
        if isPreview { return }
        do {
            enrollment = try store.read()
            enrolled = enrollment != nil
            settings.use(gateway: enrollment?.gateway)
            autoSelectionPending = settings.preferences.autoConnect
            if enrolled { reload() }
        } catch { errorMessage = "Unlock your iPhone and try again. Your saved home could not be opened." }
    }

    /// Verify new enrollment before replacing a working Keychain record.
    func enroll(gateway: String, media: String, token: String, homeName: String = "My home") {
        guard !pairing else { return }
        do {
            guard let base = URL(string: gateway.trimmingCharacters(in: .whitespacesAndNewlines)) else {
                throw EnrollmentFailure.invalid
            }
            let extra = media.trimmingCharacters(in: .whitespacesAndNewlines)
            let origin = extra.isEmpty ? nil : URL(string: extra)
            guard extra.isEmpty || origin != nil else { throw EnrollmentFailure.invalid }
            let record = Enrollment(gateway: base, mediaOrigin: origin,
                                    token: token.trimmingCharacters(in: .whitespacesAndNewlines))
            let policy = try record.policy()
            pairing = true
            pairingError = nil
            pairingTask = Task { [weak self] in
                do {
                    let values = try await GatewayClient(policy: policy)
                        .cameras(credential: BearerCredential(record.token))
                    try Task.checkCancellation()
                    guard let self else { return }
                    try self.store.save(record)
                    self.stop()
                    self.catalog?.cancel()
                    self.catalogGeneration = UUID()
                    self.busy = false
                    self.errorMessage = nil
                    self.enrollment = record
                    self.settings.use(gateway: record.gateway)
                    let name = homeName.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.settings.preferences.homeName = name.isEmpty ? "My home" : String(name.prefix(48))
                    self.cameras = values
                    self.selected = nil
                    self.catalogCheckedAt = Date()
                    self.enrolled = true
                    self.isPreview = false
                    self.pairing = false
                    self.enrollmentID = UUID()
                    self.autoSelectionPending = self.settings.preferences.autoConnect
                    self.selectInitialCamera()
                } catch {
                    guard let self, !Task.isCancelled else { return }
                    self.pairing = false
                    self.pairingError = self.message(for: error)
                }
            }
        } catch { pairingError = "Enter a valid HTTPS home address and viewer key, or import an invitation." }
    }

    func cancelPairing() { pairingTask?.cancel(); pairingTask = nil; pairing = false; pairingError = nil }

    func forget() {
        do {
            try store.remove()
            stop()
            cancelPairing()
            catalog?.cancel()
            catalogGeneration = UUID()
            busy = false
            enrollment = nil
            enrolled = false
            cameras = []
            selected = nil
            catalogCheckedAt = nil
            errorMessage = nil
            settings.use(gateway: nil)
        } catch { errorMessage = "Your saved home could not be removed. Unlock your iPhone and try again." }
    }

    func reload() {
        guard !isPreview, let enrollment else { return }
        catalog?.cancel()
        let ticket = UUID()
        catalogGeneration = ticket
        busy = true
        catalog = Task { [weak self] in
            do {
                let values = try await GatewayClient(policy: enrollment.policy())
                    .cameras(credential: BearerCredential(enrollment.token))
                try Task.checkCancellation()
                guard let self, self.catalogGeneration == ticket else { return }
                self.cameras = values
                self.busy = false
                self.catalogCheckedAt = Date()
                self.errorMessage = nil
                if let selected = self.selected, !values.contains(where: { $0.cameraID == selected.cameraID }) {
                    self.stop(); self.selected = nil
                }
                self.selectInitialCamera()
            } catch {
                guard let self, !Task.isCancelled, self.catalogGeneration == ticket else { return }
                self.busy = false
                self.errorMessage = self.message(for: error)
            }
        }
    }

    private func selectInitialCamera() {
        guard autoSelectionPending, foreground, selected == nil else { return }
        autoSelectionPending = false
        let preferred = cameras.first { $0.cameraID.value == settings.preferences.lastCameraID && $0.liveVideo }
        if let camera = preferred ?? cameras.first(where: \.liveVideo) { select(camera) }
    }

    func select(_ camera: CameraSummary) {
        if selected?.cameraID == camera.cameraID, live { return }
        stop()
        selected = camera
        settings.preferences.lastCameraID = camera.cameraID.value
        guard !isPreview else { status = "Design preview"; return }
        guard foreground, camera.liveVideo, let enrollment else { return }
        let ticket = generation
        setStatus("Connecting", live: false)
        errorMessage = nil
        playback = Task { [weak self] in await self?.receive(camera, enrollment: enrollment, ticket: ticket) }
    }

    func reconnect() {
        guard let camera = selected else { reload(); return }
        stop(); select(camera)
    }

    func setForeground(_ active: Bool) {
        foreground = active
        if !active {
            let resume = playback != nil
            stop()
            resumeWhenActive = resume
            catalog?.cancel(); catalogGeneration = UUID(); busy = false
        } else if restored {
            reload()
            if resumeWhenActive, let selected { select(selected) }
        }
        updateDisplayAwake()
    }

    func stop() {
        resumeWhenActive = false
        endRecording()
        generation = UUID()
        playback?.cancel(); playback = nil
        peer?.close()
        peer = nil
        setStatus("Paused", live: false)
        resolution = ""; framesPerSecond = 0
        updateDisplayAwake()
    }

    func updateDisplayAwake() {
        UIApplication.shared.isIdleTimerDisabled = live && foreground && settings.preferences.keepScreenAwake
    }

    func takeSnapshot() {
        guard live, !snapshotBusy, let peer, let camera = selected else { return }
        snapshotBusy = true
        let name = settings.preferences.displayName(for: camera)
        Task { [weak self] in
            guard let self else { return }
            defer { self.snapshotBusy = false }
            do {
                let data = try await peer.snapshotJPEG()
                _ = try await self.library.repository.savePhoto(data, cameraID: camera.cameraID.value, cameraName: name)
                await self.library.reload()
                self.showToast("Snapshot saved to Library")
                UINotificationFeedbackGenerator().notificationOccurred(.success)
            } catch { self.showToast(self.captureMessage(error)) }
        }
    }

    func toggleRecording() {
        if isRecording { endRecording(); return }
        guard canRecord, let peer, let camera = selected else { return }
        recordingStarting = true
        let ticket = generation
        let name = settings.preferences.displayName(for: camera)
        Task { [weak self] in
            guard let self else { return }
            defer { self.recordingStarting = false }
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
        Task { [weak self] in
            guard let self else { return }
            defer {
                self.finalizingClip = false
                if background != .invalid { UIApplication.shared.endBackgroundTask(background) }
            }
            do {
                let result = try await current.recorder.finish()
                _ = try await self.library.repository.finishClip(at: current.url, duration: result.duration,
                    cameraID: current.cameraID, cameraName: current.cameraName)
                await self.library.reload()
                self.showToast("Video clip saved to Library")
            } catch {
                try? await self.library.repository.discardClip(at: current.url)
                self.showToast(self.captureMessage(error))
            }
        }
    }

    private func setStatus(_ text: String, live: Bool) {
        if status != text { status = text }
        if self.live != live { self.live = live; updateDisplayAwake() }
    }

    private func receive(_ camera: CameraSummary, enrollment: Enrollment, ticket: UUID) async {
        guard let policy = try? enrollment.policy(), let credential = try? BearerCredential(enrollment.token) else { return }
        let api = GatewayClient(policy: policy)
        let whep = WHEPClient(policy: policy)
        for attempt in 0..<4 {
            guard !Task.isCancelled, generation == ticket, foreground else { return }
            if attempt > 0 {
                setStatus("Reconnecting · \(attempt) of 3", live: false)
                do { try await Task.sleep(nanoseconds: UInt64(1 << attempt) * 1_000_000_000) }
                catch { return }
                guard generation == ticket, foreground else { return }
            }
            let currentPeer = NativeVideoPeer()
            peer = currentPeer; videoID = UUID(); setStatus("Connecting", live: false)
            let startup = PlaybackStartup(peer: currentPeer, api: api, whep: whep) { [weak self] error in
                guard let self, self.generation == ticket, !Task.isCancelled, self.foreground else { return }
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
                    if let recording, now - recording.started >= 60 { endRecording() }
                    if currentPeer.failed || stalledAt.map({ now - $0 >= 5 }) == true { throw NativePeerError.unavailable }
                    try await Task.sleep(nanoseconds: 250_000_000)
                }
            } catch { failure = error }
            if generation == ticket { endRecording() }
            currentPeer.close()
            if generation == ticket { peer = nil; setStatus("Reconnecting", live: false) }
            await startup.close()
            guard generation == ticket, !Task.isCancelled, foreground else { return }
            if let failure, isAuthorizationFailure(failure) || attempt == 3 {
                setStatus(isAuthorizationFailure(failure) ? "Access denied" : "Offline", live: false)
                errorMessage = message(for: failure)
                return
            }
        }
    }

    private func isAuthorizationFailure(_ error: Error) -> Bool {
        guard let error = error as? MonitorError else { return false }
        return [.unauthenticated, .forbidden, .untrustedOrigin, .invalidEndpoint,
                .invalidContract, .notFound, .rateLimited].contains(error)
    }

    private func message(for error: Error) -> String {
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
        case .full: return "Your Library is full. Delete a few moments and try again."
        default:
            if (error as? NativeCaptureError) == .noCurrentFrame { return "Wait for live video before saving a moment." }
            if (error as? NativeCaptureError) == .noRecordedFrames { return "The clip was too short to save. Record a little longer." }
            return "This moment could not be saved. Check your iPhone storage and try again."
        }
    }

    func showToast(_ text: String) {
        toastTask?.cancel(); toast = text
        toastTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 3_000_000_000) } catch { return }
            self?.toast = nil
        }
    }

    #if DEBUG
    static func onboardingPreview() -> MonitorModel {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("NxrsOnboardingPreview", isDirectory: true)
        let model = MonitorModel(library: HomeLibrary(root: root))
        model.restored = true // UI tests must never read or replace an existing enrollment.
        return model
    }

    static func designPreview() -> MonitorModel {
        let defaults = UserDefaults(suiteName: "org.nxrs.home.design-preview")!
        defaults.removePersistentDomain(forName: "org.nxrs.home.design-preview")
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("NxrsDesignPreview", isDirectory: true)
        try? FileManager.default.removeItem(at: root)
        let model = MonitorModel(settings: HomeSettings(defaults: defaults), library: HomeLibrary(root: root))
        model.isPreview = true
        model.enrolled = true
        let json = #"{"api_version":"1","cameras":[{"camera_id":"living_room","gateway_id":"home","site_id":"home","name":"Living room","source":"physical","availability":"unknown","live_video":true},{"camera_id":"front_door","gateway_id":"home","site_id":"home","name":"Front door","source":"physical","availability":"unknown","live_video":true},{"camera_id":"garden","gateway_id":"home","site_id":"home","name":"Garden","source":"physical","availability":"unknown","live_video":true}]}"#
        let list = try! JSONDecoder().decode(CameraListPreview.self, from: Data(json.utf8))
        model.cameras = list.cameras
        model.selected = list.cameras.first
        model.status = "Design preview"
        model.catalogCheckedAt = Date()
        model.settings.preferences.cameras["living_room"] = CameraPersonalization(room: "Downstairs", favorite: true)
        model.settings.preferences.cameras["front_door"] = CameraPersonalization(room: "Entrance")
        model.settings.preferences.cameras["garden"] = CameraPersonalization(room: "Outdoors")
        if ProcessInfo.processInfo.arguments.contains("--preview-dark") { model.settings.preferences.appearance = .dark }
        if ProcessInfo.processInfo.arguments.contains("--preview-library") {
            // An explicitly labeled UI fixture, never a camera frame or a production seed.
            let renderer = UIGraphicsImageRenderer(size: CGSize(width: 640, height: 480))
            let image = renderer.image { context in
                UIColor.darkGray.setFill(); context.fill(CGRect(x: 0, y: 0, width: 640, height: 480))
                ("UI test fixture\nSaved photo" as NSString).draw(in: CGRect(x: 40, y: 180, width: 560, height: 160),
                    withAttributes: [.font: UIFont.systemFont(ofSize: 36), .foregroundColor: UIColor.white])
            }
            if let data = image.jpegData(compressionQuality: 0.9) {
                Task {
                    _ = try? await model.library.repository.savePhoto(data, cameraID: "ui_fixture", cameraName: "UI test fixture")
                    await model.library.reload()
                }
            }
        }
        return model
    }
    private struct CameraListPreview: Decodable { let cameras: [CameraSummary] }
    #endif
}
