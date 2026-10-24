import Foundation
import SwiftUI
import Combine
import NxrsMonitorCore
import NxrsMonitorNative

@MainActor final class MonitorModel: ObservableObject {
    @Published private(set) var enrolled = false
    @Published private(set) var cameras: [CameraSummary] = []
    @Published private(set) var selected: CameraSummary?
    @Published private(set) var sessions: [ResourceID: CameraSession] = [:]
    private var visible = Set<ResourceID>()
    private var paused = Set<ResourceID>()
    private var retired: [CameraSession] = []
    private var fullscreen: ResourceID?
    private var selectedObservation: AnyCancellable?
    var selectedSession: CameraSession? { selected.flatMap { sessions[$0.cameraID] } }
    var peer: NativeVideoPeer? { selectedSession?.peer }
    var videoID: UUID { selectedSession?.videoID ?? enrollmentID }
    var status: String { isPreview ? "Design preview" : selectedSession?.status ?? "Choose a camera" }
    var live: Bool { selectedSession?.live ?? false }
    var errorMessage: String? { catalogError ?? selectedSession?.errorMessage }
    var detectionStatus: String { selectedSession?.detectionStatus ?? "Detection unavailable" }
    var personDetected: Bool { selectedSession?.personDetected ?? false }
    var detectionHistoryIncomplete: Bool { selectedSession?.detectionHistoryIncomplete ?? false }
    @Published private var catalogError: String?
    @Published private(set) var busy = false
    @Published private(set) var pairing = false
    @Published private(set) var pairingError: String?
    @Published private(set) var enrollmentID = UUID()
    @Published private(set) var catalogCheckedAt: Date?
    var snapshotBusy: Bool { selectedSession?.snapshotBusy ?? false }
    var recordingSince: Date? { selectedSession?.recordingSince }
    var finalizingClip: Bool { selectedSession?.finalizingClip ?? false }
    var resolution: String { selectedSession?.resolution ?? "" }
    var framesPerSecond: Int { selectedSession?.framesPerSecond ?? 0 }
    @Published private(set) var toast: String?
    private(set) var isPreview = false
    let settings: HomeSettings
    let library: HomeLibrary
    private let store = EnrollmentStore()
    private var enrollment: Enrollment?
    private var catalog: Task<Void, Never>?
    private var pairingTask: Task<Void, Never>?
    private var toastTask: Task<Void, Never>?
    private var catalogGeneration = UUID()
    private var foreground = true
    private var restored = false
    private var autoSelectionPending = false
    init(settings: HomeSettings? = nil, library: HomeLibrary? = nil) {
        self.settings = settings ?? HomeSettings()
        self.library = library ?? HomeLibrary()
    }

    var gatewayHost: String { enrollment?.gateway.host ?? "No home connected" }
    var gatewayAddress: String { enrollment?.gateway.absoluteString ?? "" }
    var canRecord: Bool { selectedSession?.canRecord ?? false }
    var isRecording: Bool { selectedSession?.isRecording ?? false }

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
        } catch { catalogError = "Unlock your iPhone and try again. Your saved home could not be opened." }
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
                    self.retireAll()
                    self.catalog?.cancel()
                    self.catalogGeneration = UUID()
                    self.busy = false
                    self.catalogError = nil
                    self.enrollment = record
                    self.settings.use(gateway: record.gateway)
                    let name = homeName.trimmingCharacters(in: .whitespacesAndNewlines)
                    self.settings.preferences.homeName = name.isEmpty ? "My home" : String(name.prefix(48))
                    self.cameras = values
                    self.reconcileSessions()
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
                    self.pairingError = CameraSession.message(for: error)
                }
            }
        } catch { pairingError = "Enter a valid HTTPS home address and viewer key, or import an invitation." }
    }

    func cancelPairing() { pairingTask?.cancel(); pairingTask = nil; pairing = false; pairingError = nil }

    func forget() {
        do {
            try store.remove()
            retireAll()
            cancelPairing()
            catalog?.cancel()
            catalogGeneration = UUID()
            busy = false
            enrollment = nil
            enrolled = false
            cameras = []
            selected = nil
            catalogCheckedAt = nil
            catalogError = nil
            settings.use(gateway: nil)
        } catch { catalogError = "Your saved home could not be removed. Unlock your iPhone and try again." }
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
                self.reconcileSessions()
                self.busy = false
                self.catalogCheckedAt = Date()
                self.catalogError = nil
                if let selected = self.selected, !values.contains(where: { $0.cameraID == selected.cameraID }) {
                    self.selected = nil
                }
                self.selectInitialCamera()
                self.applyViewingPlan()
            } catch {
                guard let self, !Task.isCancelled, self.catalogGeneration == ticket else { return }
                self.busy = false
                self.catalogError = CameraSession.message(for: error)
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
        guard cameras.contains(where: { $0.cameraID == camera.cameraID }) else { return }
        selected = camera; settings.preferences.lastCameraID = camera.cameraID.value
        paused.remove(camera.cameraID); sessions[camera.cameraID]?.allowRetry()
        observeSelection(); applyViewingPlan()
    }
    func setVisible(_ camera: CameraSummary, visible value: Bool) {
        if value { visible.insert(camera.cameraID) } else { visible.remove(camera.cameraID) }
        applyViewingPlan()
    }
    func setFullscreen(_ camera: CameraSummary?) {
        fullscreen = camera?.cameraID
        if let camera { select(camera) } else { applyViewingPlan() }
    }
    func pause(_ camera: CameraSummary) { paused.insert(camera.cameraID); applyViewingPlan() }
    func resume(_ camera: CameraSummary) { select(camera) }
    func stop() { if let selected { pause(selected) } }
    func reconnect() {
        guard let selected, let session = selectedSession else { reload(); return }
        session.stop(); session.allowRetry(); paused.remove(selected.cameraID); applyViewingPlan()
    }
    func setForeground(_ active: Bool) {
        foreground = active
        if !active { catalog?.cancel(); catalogGeneration = UUID(); busy = false }
        else if restored { reload() }
        applyViewingPlan(); updateDisplayAwake()
    }
    func updateDisplayAwake() {
        UIApplication.shared.isIdleTimerDisabled = foreground && settings.preferences.keepScreenAwake && sessions.values.contains(where: \.live)
    }
    func takeSnapshot() { selectedSession?.takeSnapshot() }
    func toggleRecording() { selectedSession?.toggleRecording() }
    private func observeSelection() {
        selectedObservation = selectedSession?.objectWillChange.sink { [weak self] in
            self?.objectWillChange.send()
            // Session publishes before updating its value.
            Task { @MainActor [weak self] in self?.updateDisplayAwake() }
        }
    }
    private func makeSession(_ camera: CameraSummary) -> CameraSession {
        CameraSession(camera: camera, enrollment: enrollment, settings: settings, library: library,
            notify: { [weak self] in self?.showToast($0) },
            released: { [weak self] in self?.applyViewingPlan() },
            changed: { [weak self] in self?.updateDisplayAwake() })
    }
    private func retireAll() {
        for session in sessions.values { session.stop(); if session.occupiesSlot { retired.append(session) } }
        sessions.removeAll(); paused.removeAll(); visible.removeAll(); fullscreen = nil; selectedObservation = nil
        updateDisplayAwake()
    }
    private func reconcileSessions() {
        let allowed = Set(cameras.map(\.cameraID))
        for (id, session) in sessions where !allowed.contains(id) {
            session.stop(); if session.occupiesSlot { retired.append(session) }; sessions.removeValue(forKey: id)
        }
        for camera in cameras {
            if let session = sessions[camera.cameraID], session.camera.gatewayID == camera.gatewayID,
               session.camera.siteID == camera.siteID, session.camera.source == camera.source,
               session.camera.liveVideo == camera.liveVideo { session.update(camera) }
            else {
                if let previous = sessions[camera.cameraID] {
                    previous.stop(); if previous.occupiesSlot { retired.append(previous) }
                }
                sessions[camera.cameraID] = makeSession(camera)
            }
        }
        if let id = selected?.cameraID { selected = cameras.first { $0.cameraID == id } }
        paused.formIntersection(allowed); visible.formIntersection(allowed); observeSelection()
    }
    private func applyViewingPlan() {
        retired.removeAll { !$0.occupiesSlot }
        let requested: [ResourceID]
        if !foreground || isPreview { requested = [] }
        else if let fullscreen { requested = [fullscreen] }
        else if selected != nil {
            let ordered = cameras.map(\.cameraID).filter { visible.contains($0) }
            if let id = selected?.cameraID, ordered.contains(id) { requested = [id] + ordered.filter { $0 != id } }
            else { requested = ordered }
        } else { requested = [] }
        let admitted = requested.filter { !paused.contains($0) && sessions[$0]?.camera.liveVideo == true }
        let active = Set(sessions.filter { $0.value.active }.map(\.key))
        let retiring = Set(sessions.filter { $0.value.retiring }.map(\.key))
        let blocked = Set(sessions.filter { $0.value.blocked }.map(\.key))
        guard let plan = try? ViewingPlan(requested: admitted, active: active, retiring: retiring,
            blocked: blocked, reservedSlots: retired.count) else { return }
        for id in plan.stop { sessions[id]?.stop() }
        for id in plan.start { sessions[id]?.start() }
        updateDisplayAwake()
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
        let arguments = ProcessInfo.processInfo.arguments
        let count = arguments.contains("--preview-one-camera") ? 1 : arguments.contains("--preview-two-cameras") ? 2 : 3
        model.cameras = Array(list.cameras.prefix(count))
        model.reconcileSessions()
        model.selected = list.cameras.first
        model.observeSelection()
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
